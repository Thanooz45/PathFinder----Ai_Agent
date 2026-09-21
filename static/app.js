const form = document.querySelector("#searchForm");
const fileInput = document.querySelector("#resume");
const uploadButton = document.querySelector("#uploadButton");
const uploadLabel = document.querySelector("#uploadLabel");
const errorBox = document.querySelector("#error");
let resumeText = "";
let latestSearch = {};
let latestJobs = [];
const searchCount = document.querySelector("#searchCount");
const chatToggle = document.querySelector("#chatToggle");
const chatPanel = document.querySelector("#chatPanel");
const chatClose = document.querySelector("#chatClose");
const chatForm = document.querySelector("#chatForm");
const chatInput = document.querySelector("#chatInput");
const chatMessages = document.querySelector("#chatMessages");

fetch("/api/stats")
  .then((response) => (response.ok ? response.json() : null))
  .then((data) => { if (data) searchCount.textContent = data.search_count; })
  .catch(() => {});

uploadButton.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", async () => {
  const file = fileInput.files[0];
  if (!file) return;
  if (file.type !== "application/pdf")
    return showError("Please choose a PDF resume.");

  uploadLabel.textContent = "Reading your resume…";
  const body = new FormData();
  body.append("file", file);

  try {
    const response = await fetch("/api/resume", { method: "POST", body });
    const data = await response.json();
    if (!response.ok) throw Error(data.detail);
    resumeText = data.text;
    uploadLabel.textContent = `Resume added: ${data.filename}`;
  } catch (error) {
    uploadLabel.textContent = "Add resume for personalised insight";
    showError(error.message || "Could not read your resume.");
  }
});

function selectedValues(name) {
  return [...document.querySelectorAll(`input[name="${name}"]:checked`)].map(
    (input) => input.value,
  );
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.add("show");
}

function escapeHTML(value) {
  const element = document.createElement("div");
  element.textContent = value;
  return element.innerHTML;
}

function escapeAttribute(value) {
  return escapeHTML(value).replace(/&quot;/g, "&#34;");
}

function safeAdviceHTML(html) {
  const allowedTags = new Set(["H3", "H4", "P", "UL", "LI", "STRONG"]);
  const documentFragment = new DOMParser().parseFromString(html, "text/html");
  documentFragment.body.querySelectorAll("*").forEach((element) => {
    if (!allowedTags.has(element.tagName)) {
      element.replaceWith(...element.childNodes);
      return;
    }
    [...element.attributes].forEach((attribute) => element.removeAttribute(attribute.name));
  });
  return documentFragment.body.innerHTML;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.classList.remove("show");

  const employmentTypes = selectedValues("employment");
  const workModes = selectedValues("workMode");
  if (!employmentTypes.length || !workModes.length) {
    return showError("Select at least one employment type and one work mode.");
  }

  const button = form.querySelector(".submit");
  button.disabled = true;
  button.querySelector("span").textContent = "Searching…";
  const skill = document.querySelector("#skill").value;
  const location = document.querySelector("#location").value;
  const experienceLevel = document.querySelector("#experienceLevel").value;
  const minimumPay = Number(document.querySelector("#minimumPay").value) || 0;

  try {
    const response = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        skill,
        location,
        resume_text: resumeText,
        employment_types: employmentTypes,
        work_modes: workModes,
        experience_level: experienceLevel,
        minimum_pay: minimumPay,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || "Search failed.");
    renderResults(data);
    searchCount.textContent = data.search_count;
  } catch (error) {
    showError(error.message || "Something went wrong. Please try again.");
  } finally {
    button.disabled = false;
    button.querySelector("span").textContent = "Explore live jobs";
  }
});

function renderResults(data) {
  latestSearch = data.query;
  latestJobs = data.jobs;
  const title = document.querySelector("#resultTitle");
  title.innerHTML = `${escapeHTML(data.query.skill)} <em>in ${escapeHTML(data.query.location)}</em>`;
  document.querySelector("#advice").innerHTML = safeAdviceHTML(data.advice);
  document.querySelector("#jobSource").textContent = data.live_jobs
    ? "LIVE VIA JSEARCH / RAPIDAPI"
    : "ADD RAPIDAPI KEY FOR LIVE LISTINGS";

  const jobs = document.querySelector("#jobs");
  jobs.innerHTML = data.jobs.length
    ? data.jobs
        .map(
          (job) => `
      <div class="job">
        <div>
          <h3>${escapeHTML(job.title)}</h3>
          <p>${escapeHTML(job.company)} · ${escapeHTML(job.location)}</p>
          <div class="job-meta">
            <span>${escapeHTML(job.type)}</span>
            <span>${escapeHTML(job.work_mode)}</span>
            <span>${escapeHTML(job.posted)}</span>
            <span>${escapeHTML(job.salary)}</span>
          </div>
          <details class="job-details">
            <summary>Role overview &amp; employer highlights</summary>
            <p>${escapeHTML(job.description)}</p>
            ${job.highlights.length ? `<ul>${job.highlights.map((highlight) => `<li>${escapeHTML(highlight)}</li>`).join("")}</ul>` : ""}
            <p class="listing-source">Listing source: ${escapeHTML(job.source || "JSearch via RapidAPI")}</p>
          </details>
        </div>
        ${job.link ? `<a target="_blank" rel="noopener noreferrer" href="${escapeAttribute(job.link)}">APPLY ↗</a>` : ""}
      </div>`,
        )
        .join("")
    : '<p class="empty">No matching live vacancies were returned. Try another location or broaden your filters.</p>';

  const results = document.querySelector("#results");
  results.hidden = false;
  // On phones, the career brief can be much taller than the viewport. Put the
  // first live role in view instead of making users hunt below that brief.
  const mobile = window.matchMedia("(max-width: 780px)").matches;
  const destination = mobile && data.jobs.length ? jobs.closest("article") : results;
  destination.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setChatOpen(open) {
  chatPanel.hidden = !open;
  chatToggle.setAttribute("aria-expanded", String(open));
  if (open) chatInput.focus();
}

function addChatMessage(message, role) {
  const element = document.createElement("div");
  element.className = `chat-message ${role}`;
  element.textContent = message;
  chatMessages.append(element);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return element;
}

chatToggle.addEventListener("click", () => setChatOpen(chatPanel.hidden));
chatClose.addEventListener("click", () => setChatOpen(false));
chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  addChatMessage(message, "user");
  chatInput.value = "";
  const pending = addChatMessage("Thinking…", "assistant pending");
  const button = chatForm.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, search_context: latestSearch, jobs: latestJobs }),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.detail || "Chat is unavailable.");
    pending.textContent = data.reply;
  } catch (error) {
    pending.textContent = "I couldn't respond just now. Please try again.";
  } finally {
    pending.classList.remove("pending");
    button.disabled = false;
  }
});

document.querySelectorAll(".search-panel, .advice-card, .jobs-card").forEach((card) => {
  card.addEventListener("pointermove", (event) => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const rect = card.getBoundingClientRect();
    const x = (event.clientX - rect.left) / rect.width - 0.5;
    const y = (event.clientY - rect.top) / rect.height - 0.5;
    card.style.setProperty("--tilt-x", `${-y * 4}deg`);
    card.style.setProperty("--tilt-y", `${x * 5}deg`);
  });
  card.addEventListener("pointerleave", () => {
    card.style.setProperty("--tilt-x", "0deg");
    card.style.setProperty("--tilt-y", "0deg");
  });
});
