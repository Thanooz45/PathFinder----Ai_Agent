import os
import re
import uuid
import asyncio
import json
import html
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from pydantic import BaseModel, Field
from pypdf import PdfReader
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ReturnDocument

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
STATS_FILE = BASE_DIR / ".search_stats.json"
UPLOAD_DIR.mkdir(exist_ok=True)

try:
    search_count = max(0, int(json.loads(STATS_FILE.read_text(encoding="utf-8")).get("search_count", 0)))
except (OSError, ValueError, json.JSONDecodeError):
    search_count = 0
search_count_lock = asyncio.Lock()
mongo_client: AsyncIOMotorClient | None = None
search_counter_collection = None


async def persist_local_search_count() -> None:
    STATS_FILE.write_text(json.dumps({"search_count": search_count}), encoding="utf-8")


async def increment_search_count() -> int:
    """Atomically increment the shared MongoDB count, with a local fallback."""
    global search_count
    async with search_count_lock:
        if search_counter_collection is not None:
            try:
                counter = await search_counter_collection.find_one_and_update(
                    {"_id": "all_searches"},
                    {"$inc": {"count": 1}},
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                search_count = max(0, int(counter.get("count", 0)))
                return search_count
            except Exception:
                # A temporary database outage should not prevent a job search.
                pass
        search_count += 1
        await persist_local_search_count()
        return search_count


@asynccontextmanager
async def lifespan(_: FastAPI):
    global mongo_client, search_counter_collection, search_count
    uri = os.getenv("MONGODB_URI", "").strip()
    if uri:
        try:
            mongo_client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=3000)
            await mongo_client.admin.command("ping")
            database_name = os.getenv("MONGODB_DATABASE", "pathfinder")
            search_counter_collection = mongo_client[database_name]["counters"]
            counter = await search_counter_collection.find_one({"_id": "all_searches"})
            if counter:
                search_count = max(0, int(counter.get("count", 0)))
        except Exception:
            if mongo_client is not None:
                mongo_client.close()
            mongo_client = None
            search_counter_collection = None
    yield
    if mongo_client is not None:
        mongo_client.close()


app = FastAPI(title="Pathfinder Career AI", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class SearchRequest(BaseModel):
    skill: str = Field(min_length=2, max_length=100)
    location: str = Field(min_length=2, max_length=100)
    resume_text: str = Field(default="", max_length=30000)
    employment_types: list[str] = Field(default_factory=lambda: ["FULLTIME", "INTERN"])
    work_modes: list[str] = Field(default_factory=lambda: ["REMOTE", "ONSITE"])
    experience_level: str = Field(default="FRESHER", max_length=30)
    minimum_pay: int = Field(default=0, ge=0, le=100000000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=1200)
    search_context: dict = Field(default_factory=dict)
    jobs: list[dict] = Field(default_factory=list, max_length=8)


def clean_text(value: str, limit: int = 24000) -> str:
    return re.sub(r"\s+", " ", value).strip()[:limit]


def clean_listing_text(value: str, limit: int = 24000) -> str:
    """Turn partner-provided HTML job descriptions into readable plain text."""
    value = re.sub(r"<\s*(?:style|script)[^>]*>.*?<\s*/\s*(?:style|script)\s*>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html.unescape(value), limit)


def days_since_posted(value: str | None, fallback: str | None) -> str:
    if value:
        try:
            posted_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            days = max(0, (datetime.now(timezone.utc) - posted_at).days)
            return "Posted today" if days == 0 else f"Posted {days} day{'s' if days != 1 else ''} ago"
        except ValueError:
            pass
    return fallback or "Posting date unavailable"


def pay_range(item: dict) -> tuple[str, float]:
    minimum = item.get("job_min_salary")
    maximum = item.get("job_max_salary")
    currency = item.get("job_salary_currency") or "INR"
    period = item.get("job_salary_period") or "year"
    values = [value for value in (minimum, maximum) if isinstance(value, (int, float))]
    if not values:
        return "Pay not disclosed", 0
    prefix = "INR " if currency.upper() == "INR" else f"{currency} "
    if minimum and maximum:
        return f"{prefix}{minimum:,.0f} - {maximum:,.0f} / {period}", float(maximum)
    return f"{prefix}{values[0]:,.0f} / {period}", float(values[0])


async def fetch_jobs(
    skill: str,
    location: str,
    employment_types: list[str],
    work_modes: list[str],
    experience_level: str,
    minimum_pay: int,
) -> list[dict]:
    key = os.getenv("RAPIDAPI_KEY")
    if not key:
        return []
    allowed_types = {"FULLTIME", "PARTTIME", "CONTRACTOR", "INTERN"}
    selected_types = [item for item in employment_types if item in allowed_types]
    selected_modes = {item for item in work_modes if item in {"REMOTE", "ONSITE"}}
    headers = {
        "x-rapidapi-key": key,
        "x-rapidapi-host": "jsearch.p.rapidapi.com",
    }
    params = {
        "query": f"{skill} jobs in {location}",
        "num_pages": "1",
        "country": "in",
        "employment_types": ",".join(selected_types or ["FULLTIME", "INTERN"]),
        "date_posted": "all",
    }
    if selected_modes == {"REMOTE"}:
        params["remote_jobs_only"] = "true"
    if experience_level == "FRESHER":
        params["job_requirements"] = "no_experience"
    elif experience_level == "ONE_TWO":
        params["job_requirements"] = "under_3_years_experience"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get("https://jsearch.p.rapidapi.com/search-v2", headers=headers, params=params)
            response.raise_for_status()
        raw = response.json().get("data", {}).get("jobs", [])
        jobs = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            is_remote = bool(item.get("job_is_remote"))
            if selected_modes == {"REMOTE"} and not is_remote:
                continue
            if selected_modes == {"ONSITE"} and is_remote:
                continue
            salary, pay_ceiling = pay_range(item)
            if minimum_pay and pay_ceiling and pay_ceiling < minimum_pay:
                continue
            raw_highlights = item.get("job_highlights") or {}
            highlights = []
            if isinstance(raw_highlights, dict):
                for group in raw_highlights.values():
                    if isinstance(group, list):
                        highlights.extend(clean_text(str(text), 100) for text in group[:2])
            jobs.append({
                "title": item.get("job_title") or "Untitled role",
                "company": item.get("employer_name") or "Company not listed",
                "location": item.get("job_city") or item.get("job_location") or item.get("job_country") or location,
                "type": item.get("job_employment_type") or "Full-time",
                "work_mode": "Remote" if is_remote else "On-site",
                "posted": days_since_posted(item.get("job_posted_at_datetime_utc"), item.get("job_posted_human_readable")),
                "salary": salary,
                "description": clean_listing_text(item.get("job_description") or "No role description was supplied by the employer.", 800),
                "highlights": highlights[:3],
                "link": item.get("job_apply_link") or "",
                "source": clean_text(str(item.get("job_publisher") or "JSearch via RapidAPI"), 100),
            })
            if len(jobs) == 8:
                break
        return jobs
    except (httpx.HTTPError, ValueError):
        return []


def fallback_advice(skill: str, location: str, has_resume: bool) -> str:
    resume_line = (
        "I can see the resume context you provided—use the gap list below to tailor it."
        if has_resume
        else "Upload a resume next time for a personalised fit assessment."
    )
    return f"""<h3>{skill.title()} is a strong skill to explore</h3>
<p>Hiring teams commonly look for proof that you can apply <strong>{skill}</strong> to a real business or technical problem—not just a course certificate. Focus your search on {location} as well as remote roles.</p>
<h4>Resume essentials for this role</h4><ul><li>A focused headline, contact details, portfolio or LinkedIn link, and skills relevant to {skill}.</li><li>One or two projects with the problem, tools used, your contribution, and a measurable result.</li><li>Relevant coursework, certifications, and keywords from the roles you choose to apply for.</li></ul>
<h4>Practical next steps</h4><ul><li>Build one focused portfolio project with a clear problem, outcome, and GitHub/demo link.</li><li>Mirror the language in job descriptions in your resume headline and skills section.</li><li>Prepare two concise stories about your work: the challenge, your contribution, and the result.</li></ul>
<p>{resume_line}</p>"""


async def generate_advice(
    skill: str, location: str, experience_level: str, resume_text: str
) -> str:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return fallback_advice(skill, location, bool(resume_text))
    prompt = f"""You are a precise career coach. Provide concise, practical guidance for a {experience_level} candidate seeking {skill} jobs in {location}. Return ONLY clean HTML using h3, h4, p, ul, li and strong tags. Include market outlook, 3 valued capabilities, a 'Resume essentials for this role' section, and a 'Resume fit and gaps' section. If resume context is supplied, evaluate it against typical real job requirements: cite only evidence actually in it, then list concrete missing sections or skills. If no resume is supplied, explain the baseline resume content required. Never claim to have searched live job data and do not invent experience. Resume context: {resume_text or 'None provided'}"""
    try:
        client = genai.Client(api_key=key)
        result = await client.aio.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        text = result.text or ""
        return text if text else fallback_advice(skill, location, bool(resume_text))
    except Exception:
        return fallback_advice(skill, location, bool(resume_text))


def simple_question_reply(question: str) -> str | None:
    if "capital" in question and ("india" in question or "nidia" in question):
        return "The capital of India is New Delhi."
    if "capital" in question and ("telangana" in question or "teangana" in question):
        return "The capital of Telangana is Hyderabad."
    if "wfh" in question or "work from home" in question:
        return "WFH means Work From Home. It usually means you can work remotely rather than from an office. Check each job description because some employers use it for fully remote roles while others expect occasional office visits."
    return None


def fallback_chat(message: str, context: dict, jobs: list[dict]) -> str:
    question = clean_text(message, 1200).lower()
    skill = clean_text(str(context.get("skill", "your target role")), 100)
    location = clean_text(str(context.get("location", "your preferred location")), 100)
    simple_reply = simple_question_reply(question)
    if simple_reply:
        return simple_reply
    index = referenced_job_index(question, len(jobs))
    if index is not None and 0 <= index < len(jobs):
        job = jobs[index]
        company = clean_text(str(job.get("company") or "The employer"), 120)
        title = clean_text(str(job.get("title") or "this role"), 150)
        details = clean_text(str(job.get("description") or "No employer description was supplied."), 500)
        return f"Job {index + 1} is {title} at {company}. It is listed as {job.get('type') or 'an unspecified role'} in {job.get('location') or location}, with {job.get('work_mode') or 'an unspecified work mode'} work and {job.get('salary') or 'pay not disclosed'}. About the company/role from the listing: {details}"
    titles = ", ".join(clean_text(str(job.get("title", "")), 80) for job in jobs[:3] if job.get("title"))
    if titles:
        return f"For your {skill} search in {location}, I can help you compare the roles shown: {titles}. Match your strongest projects to the requirements, then tailor your resume headline and top skills before applying. What would you like to assess—fit, resume wording, interview prep, or a specific role?"
    return f"For {skill} roles in {location}, focus on evidence of relevant skills: a clear project, measurable outcome, and the tools you used. Ask me about resume tailoring, applications, interview preparation, or career paths."


def job_reference_index(question: str) -> int | None:
    """Return the zero-based displayed-job index when the user identifies a job."""
    ordinal_match = re.search(r"\b(?:job\s*(?:number\s*)?)?(\d+)(?:st|nd|rd|th)\s*(?:job|role)?\b", question)
    if ordinal_match:
        return int(ordinal_match.group(1)) - 1
    numeric_match = re.search(r"\b(?:job|role)\s*(?:number\s*)?(\d+)\b", question)
    if numeric_match:
        return int(numeric_match.group(1)) - 1
    ordinal_words = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5, "seventh": 6, "eighth": 7}
    return next((value for word, value in ordinal_words.items() if re.search(rf"\b{word}\b", question)), None)


def referenced_job_index(question: str, job_count: int) -> int | None:
    """Resolve relative references such as 'second-last job' before ordinals."""
    if re.search(r"\b(?:second|2nd)\s*(?:to\s*)?last\b", question):
        return job_count - 2
    if re.search(r"\b(?:last|final)\s+(?:job|role|company)\b", question):
        return job_count - 1
    return job_reference_index(question)


def compare_listings_reply(jobs: list[dict]) -> str | None:
    if not jobs:
        return "Search for jobs first, then I can compare the displayed companies and roles."
    summaries = []
    for index, job in enumerate(jobs[:5]):
        summaries.append(
            f"{index + 1}. {clean_text(str(job.get('company') or 'Company not listed'), 55)} — "
            f"{clean_text(str(job.get('title') or 'role not listed'), 70)} "
            f"({job.get('work_mode') or 'mode not listed'}, {job.get('location') or 'location not listed'})"
        )
    return (
        "There is no objectively 'best' company from the listing data alone—company culture, pay, and growth details are not consistently provided. "
        "Choose the role that best matches your experience and preferred work mode:\n\n" + "\n".join(summaries) +
        "\n\nOpen the role overview and application page to verify the employer, requirements, and work arrangement before applying."
    )


def job_context_reply(message: str, context: dict, jobs: list[dict]) -> str | None:
    """Ground displayed-job questions in RapidAPI data before using a generative model."""
    index = referenced_job_index(clean_text(message, 1200).lower(), len(jobs))
    if index is None:
        return None
    if index < 0 or index >= len(jobs):
        return f"I can see {len(jobs)} displayed job{'s' if len(jobs) != 1 else ''}. Please choose a number in that range."
    job = jobs[index]
    title = clean_text(str(job.get("title") or "this role"), 150)
    company = clean_text(str(job.get("company") or "Company not listed"), 120)
    location = clean_text(str(job.get("location") or context.get("location") or "location not listed"), 120)
    description = clean_text(str(job.get("description") or "No employer description was supplied by the source."), 650)
    highlights = [clean_text(str(item), 130) for item in (job.get("highlights") or [])[:3] if item]
    answer = f"Job {index + 1} is {title} at {company}. It is {job.get('type') or 'a listed role'}, {job.get('work_mode') or 'work mode not listed'}, in {location}. Compensation: {job.get('salary') or 'not disclosed'}.\n\nWhat the listing says: {description}"
    if highlights:
        answer += "\n\nKey highlights: " + "; ".join(highlights) + "."
    source = clean_text(str(job.get("source") or "JSearch via RapidAPI"), 100)
    return answer + f"\n\nSource: {source}, delivered through JSearch on RapidAPI. This listing does not include a broader company profile."


async def generate_chat_reply(message: str, context: dict, jobs: list[dict]) -> str:
    # A concrete job question should never be replaced by generic AI career advice.
    direct_job_reply = job_context_reply(message, context, jobs)
    if direct_job_reply:
        return direct_job_reply
    question = clean_text(message, 1200).lower()
    simple_reply = simple_question_reply(question)
    if simple_reply:
        return simple_reply
    if jobs and re.search(r"\b(?:which|what)\s+(?:company|employer).{0,35}\b(?:best|better)\b|\bbest\s+(?:company|employer)\b", question):
        return compare_listings_reply(jobs) or fallback_chat(message, context, jobs)
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return fallback_chat(message, context, jobs)
    safe_jobs = [{field: clean_text(str(job.get(field, "")), 400) for field in ("title", "company", "location", "type", "work_mode", "salary", "description")} for job in jobs[:8]]
    prompt = f"""You are Pathfinder's concise, helpful career assistant. Answer career, resume, interview, listed-job questions and simple general-knowledge questions directly. For a numbered job, use its position in Displayed jobs (for example, the fifth job is index 5). Use only supplied job details for companies and roles; say when information is missing. Never claim a job is still available, invent details, or guarantee an outcome. Keep the reply under 160 words in plain text and practical. User question: {clean_text(message, 1200)} Search context: {context} Displayed jobs: {safe_jobs}"""
    try:
        client = genai.Client(api_key=key)
        result = await client.aio.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        return clean_text(result.text or "", 1800) or fallback_chat(message, context, jobs)
    except Exception:
        return fallback_chat(message, context, jobs)


@app.get("/")
async def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/stats")
async def stats():
    return {"search_count": search_count}


@app.post("/api/chat")
async def chat(request: ChatRequest):
    return {"reply": await generate_chat_reply(request.message, request.search_context, request.jobs)}


@app.post("/api/resume")
async def upload_resume(file: UploadFile = File(...)):
    if file.content_type not in {"application/pdf", "application/x-pdf"}:
        raise HTTPException(400, "Please upload a PDF resume.")
    content = await file.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(413, "Resume must be smaller than 8 MB.")
    destination = UPLOAD_DIR / f"{uuid.uuid4()}.pdf"
    destination.write_bytes(content)
    try:
        reader = PdfReader(str(destination))
        text = clean_text(" ".join(page.extract_text() or "" for page in reader.pages))
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(400, "We couldn't read that PDF. Please try another file.") from exc
    destination.unlink(missing_ok=True)
    if not text:
        raise HTTPException(400, "No readable text was found in this PDF.")
    return {"text": text, "filename": file.filename}


@app.post("/api/search")
async def search(request: SearchRequest):
    skill = clean_text(request.skill, 100)
    location = clean_text(request.location, 100)
    # These requests are independent. Running them together makes a mobile
    # search feel much faster, especially when the AI provider is slow.
    advice, jobs = await asyncio.gather(
        generate_advice(
            skill,
            location,
            request.experience_level,
            clean_text(request.resume_text),
        ),
        fetch_jobs(
            skill,
            location,
            request.employment_types,
            request.work_modes,
            request.experience_level,
            request.minimum_pay,
        ),
    )
    current_count = await increment_search_count()
    return {
        "advice": advice,
        "jobs": jobs,
        "live_jobs": bool(os.getenv("RAPIDAPI_KEY")),
        "query": {"skill": skill, "location": location},
        "search_count": current_count,
    }
