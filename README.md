# ResuMap — AI Job Application Automation Engine

ResuMap is an end-to-end job application automation system running on an Azure VM. It finds relevant job openings, tailors your resume for each one, and autonomously fills and submits the application forms — including handling React-Select dropdowns, file uploads, email verification codes, and EEO fields.

---

## System Architecture

```
Replit Dashboard (User-facing web app)
    │
    │  GET /api/webhooks/users/active  (fetch active users + profiles)
    │  POST /api/webhooks/application  (push completed application results)
    ▼
Azure VM  ──────────────────────────────────────────────────────────────
    │
    ├── scout.py        Finds jobs, scores them, orchestrates the pipeline
    ├── tailor.py       Optimizes the resume text for each specific job
    ├── generate_pdf.py Produces a polished PDF from the optimized resume
    └── executor.py     Autonomously fills and submits the application form
```

**Flow per user per run:**

1. **Scout** fetches active users and their profiles from Replit
2. **Scout** searches Adzuna for relevant job listings, filters to supported ATS platforms (Greenhouse, Lever, Ashby), scores each job for fit using Azure OpenAI
3. Already-applied jobs are filtered out (deduplication via `applications_log.jsonl`) **before** top-N selection so the pipeline always fills the quota with fresh candidates
4. For each top job, **Tailor** rewrites the resume content to match the job description and **generate_pdf** renders it to PDF
5. **Executor** navigates to the job application form using Playwright + Stagehand, fills it end-to-end, and submits
6. The result (application data + Q&A + match score) is POSTed to the Replit dashboard webhook

---

## Key Components

### `scout.py`
- Fetches user profiles and resumes from Replit via `GET /api/webhooks/users/active`
- Searches Adzuna API for job listings matching the user's target role and location
- Resolves redirect URLs to find the actual ATS (Greenhouse, Lever, Ashby, Workable)
- Scores each job 0–100 using Azure OpenAI (GPT-4.1) against the user's resume
- Applies domain bonuses/penalties: gold-standard ATS platforms get +10, scrapers get -10
- Filters already-applied jobs using `applications_log.jsonl` **before** selecting top-N
- Calls `tailor.py` → `generate_pdf.py` → `executor.py` for each selected job
- Passes `match_score`, `relevance_explanation`, and `user_id` to executor for webhook reporting

### `executor.py`
The core automation engine. Runs a headless Chromium browser with Playwright and Stagehand (AI-powered browser control). Key capabilities:

- **Phase 0 — ATS URL resolution:** Queries Greenhouse, Lever, and Ashby public APIs to find the direct application form URL, bypassing Adzuna redirect chains
- **Phase 1 — Identity fields:** Natively fills first name, last name, email, phone, LinkedIn, website using predictable `<input>` selectors across all ATS platforms
- **Phase 2 — Resume upload:** Detects file input elements and uploads the tailored PDF natively
- **Phase 3 — React-Select dropdowns:** Discovers all unselected React-Select containers, opens each to read available options, sends all fields in a single LLM call (OpenAI), then clicks each answer natively via Playwright — avoiding Stagehand's false-success problem with React synthetic events. Covers EEO fields (gender, race, veteran status, disability) with sensible defaults when profile values are absent.
- **Phase 4 — Text inputs:** Finds empty text inputs (skipping identity/React-Select internals), sends all fields to LLM for answers, fills natively with realistic typing delay
- **Phase 5 — City autocomplete:** Handles Greenhouse's async geocode API — types, waits 2.2s for API response, clicks the best matching suggestion
- **Phase 6 — Submit:** Presses Escape to close any open dropdowns, tries native Playwright button click, falls back to Stagehand act
- **Phase 7 — Email verification:** Polls Gmail via IMAP for Greenhouse's post-submit security code (handles both numeric and alphanumeric codes), fills it natively into the verification fieldset
- **Phase 8 — Logging:** Appends result to `applications_log.jsonl` for deduplication; POSTs full payload (Q&A, score, status) to Replit webhook at `POST /api/webhooks/application`

### `tailor.py`
- Extracts structured data from the PDF resume using pdfplumber
- Makes 3 sequential OpenAI calls to: (1) score and analyze fit, (2) rewrite bullet points for the target role, (3) produce a final structured JSON resume
- Returns a structured resume dict ready for PDF generation

### `generate_pdf.py`
- Renders the structured resume JSON into a polished, ATS-friendly PDF using ReportLab
- Uses the Inter font family for clean typesetting
- Outputs to the `resumes/` directory with naming convention `{userId}_{Company}_{Role}.pdf`

### `server.py` / `main.py`
- FastAPI service exposing `/health`, `/extract`, and `/tailor` endpoints
- Used when tailor runs as a standalone microservice (alternative to in-process calls)

---

## Deduplication

Every application attempt is appended to `applications_log.jsonl` (one JSON line per attempt):

```json
{"ts": 1773779529, "user_email": "user@example.com", "job_url": "https://...", "company": "Acme", "job_title": "Senior PM", "submitted": true}
```

On each scout run, the log is read **before** top-N selection. Jobs with `submitted: true` for the same user are filtered out of the scored pool so the top-N always draws from fresh candidates. Only `submitted: true` records are skipped — failed attempts are retried on the next run.

---

## Replit Webhook Integration

After each successful submission, executor POSTs to the Replit dashboard:

```
POST {REPLIT_URL}/api/webhooks/application
X-Webhook-Secret: <secret>

{
  "userId": "42",
  "jobTitle": "Staff Product Manager",
  "company": "Calendly",
  "status": "Applied",
  "jobUrl": "https://job-boards.greenhouse.io/calendly/jobs/...",
  "matchScore": 87,
  "relevanceExplanation": "Strong PM background with AI product experience...",
  "questionsAnswers": [
    {"question": "Are you authorized to work in the US?", "answer": "Yes"},
    {"question": "Will you require sponsorship?", "answer": "No"}
  ]
}
```

Only successful submissions (`submitted: true`) trigger the webhook — failed attempts are not reported.

Active users are fetched via:

```
GET {REPLIT_URL}/api/webhooks/users/active
X-Webhook-Secret: <secret>
```

---

## Environment Variables (`.env`)

```env
# Replit integration
REPLIT_URL=https://resumap.site
WEBHOOK_SECRET=your-webhook-secret

# Azure OpenAI (used by scout + tailor for scoring and resume optimization)
AZURE_OPENAI_KEY=...
AZURE_OPENAI_ENDPOINT=https://your-resource.cognitiveservices.azure.com/...
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4.1
AZURE_OPENAI_VERSION=2024-02-01

# OpenAI (used by executor for React-Select + text field LLM passes)
EXECUTOR_OPENAI_KEY=sk-...
EXECUTOR_MODEL=gpt-4o-mini

# Adzuna job search API
ADZUNA_APP_ID=...
ADZUNA_APP_KEY=...

# Gmail IMAP (for reading Greenhouse email verification codes)
GMAIL_APP_PASSWORD=...
```

---

## Supported ATS Platforms

| Platform | Apply URL Pattern | Notes |
|----------|-------------------|-------|
| Greenhouse | `job-boards.greenhouse.io` | Most common; supports email verification gate |
| Lever | `jobs.lever.co` | No verification gate |
| Ashby | `jobs.ashbyhq.com` | Similar to Greenhouse |
| Workable | `apply.workable.com` | Basic support |

---

## File Structure

```
tailor-service/
├── scout.py              # Job search, scoring, deduplication, pipeline orchestration
├── executor.py           # Browser automation: form fill + submit + webhook reporting
├── tailor.py             # Resume optimization (3-call OpenAI pipeline)
├── generate_pdf.py       # PDF rendering (ReportLab + Inter fonts)
├── server.py             # FastAPI microservice wrapper for tailor endpoints
├── main.py               # Alternative entry point / API server
├── extract.py            # PDF text extraction utilities
├── apply.py              # Legacy Skyvern-based applier (superseded by executor.py)
├── fonts/                # Inter font family TTF files
├── requirements.txt      # Python dependencies
├── .env.example          # Environment variable template
└── README.md             # This file
```

---

## Running the Pipeline

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Single full run (scout → tailor → executor for all active users)
python scout.py

# Run as a background service
nohup python scout.py >> output.log 2>&1 &
```

---

## Dependencies

- **Playwright** — headless browser automation
- **Stagehand** — AI-powered browser control layer (Playwright wrapper)
- **playwright-stealth** — anti-bot fingerprint evasion
- **OpenAI / Azure OpenAI** — LLM calls for job scoring, dropdown answers, text field answers
- **pdfplumber / PyMuPDF** — PDF text extraction
- **ReportLab** — PDF generation
- **FastAPI / Uvicorn** — microservice API layer
- **Requests** — HTTP client for Adzuna API and Replit webhooks
