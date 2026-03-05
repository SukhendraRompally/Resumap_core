Tailor Service
A standalone Python microservice that optimizes resumes for specific job descriptions. This is the Tailor layer of the ResuMap automation pipeline.

Given a PDF resume and a job description, it returns an optimized PDF and a full analysis — ready for the Applier to use.

How it fits in the pipeline
Scout (Azure VM)
    → finds top 10 jobs for the user
    → calls Tailor for each job
Tailor (this service, Azure VM)
    → extracts text from PDF resume
    → calls OpenAI (3 times) to optimize resume for the job
    → generates a polished PDF
    → returns PDF + analysis JSON
Applier (Azure VM)
    → receives optimized PDFs + analysis from Tailor
    → uses ResuMap webhook API to log applications
    → autonomously submits applications
Prerequisites
Python 3.11+
pip
An OpenAI API key (GPT-4o access required)
Setup
1. Copy the folder to your VM
scp -r tailor-service/ user@your-vm-ip:~/tailor-service/
Or clone/pull the full repo and navigate to tailor-service/.

2. Install dependencies
cd tailor-service
pip install -r requirements.txt
3. Configure environment variables
cp .env.example .env
nano .env
Fill in:

OPENAI_API_KEY=sk-your-key-here
TAILOR_SECRET=a-long-random-secret-shared-with-scout-and-applier
PORT=8000
TAILOR_SECRET is a shared secret that Scout and Applier must send in the X-Tailor-Secret header on every request. Pick any long random string.

4. Run the service
uvicorn server:app --host 0.0.0.0 --port 8000
For production with multiple workers:

uvicorn server:app --host 0.0.0.0 --port 8000 --workers 2
Running as a systemd service (Azure VM)
Create /etc/systemd/system/tailor.service:

[Unit]
Description=Tailor Resume Optimization Service
After=network.target
[Service]
Type=simple
User=azureuser
WorkingDirectory=/home/azureuser/tailor-service
EnvironmentFile=/home/azureuser/tailor-service/.env
ExecStart=/usr/bin/python3 -m uvicorn server:app --host 0.0.0.0 --port 8000 --workers 2
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
Then enable and start it:

sudo systemctl daemon-reload
sudo systemctl enable tailor
sudo systemctl start tailor
sudo systemctl status tailor
API Reference
GET /health
Liveness check.

curl http://localhost:8000/health
Response:

{"status": "ok", "service": "tailor"}
POST /extract
Extract raw text from a PDF resume (useful for debugging).

curl -X POST http://localhost:8000/extract \
  -H "X-Tailor-Secret: your-secret-here" \
  -F "pdf_file=@/path/to/resume.pdf"
Response:

{
  "text": "John Smith\njohn@example.com | +1 (555) 000-0000\n\nEXPERIENCE\n...",
  "layout": [...]
}
Error responses:

400 — not a PDF, unreadable PDF, or not a resume
401 — missing or wrong X-Tailor-Secret
POST /tailor
The main endpoint. Optimizes a resume for a specific job description.

curl -X POST http://localhost:8000/tailor \
  -H "X-Tailor-Secret: your-secret-here" \
  -F "pdf_file=@/path/to/resume.pdf" \
  -F "job_description=We are hiring a Senior Product Manager to lead our growth team. Responsibilities include defining product strategy, working with engineering and design, and driving key metrics. Requirements: 5+ years PM experience, strong analytical skills, experience with B2B SaaS products."
Response:

{
  "pdf_base64": "JVBERi0xLjQK...",
  "filename": "resume_optimized.pdf",
  "analysis": {
    "overallScore": 78,
    "missingSkills": ["B2B SaaS", "growth metrics", "stakeholder alignment"],
    "suggestedChanges": [
      {
        "original": "Led product roadmap for mobile app",
        "improved": "Drove product strategy and roadmap for mobile app, increasing user retention by 23%",
        "reason": "Adds quantified impact and aligns with 'driving key metrics' requirement"
      }
    ],
    "structuredResume": { ... },
    "fullOptimizedText": "John Smith\n..."
  }
}
To decode and save the PDF:

import base64
with open("optimized.pdf", "wb") as f:
    f.write(base64.b64decode(response["pdf_base64"]))
Error responses:

400 — not a PDF, unreadable PDF, not a resume, or JD too short/unrecognized
401 — missing or wrong X-Tailor-Secret
500 — OpenAI failure or PDF generation failure
Integration guide (for Scout/Applier)
Scout finds a relevant job and has the user's resume PDF path
Scout calls Tailor:
import httpx, base64
async with httpx.AsyncClient(timeout=120) as client:
    with open("resume.pdf", "rb") as f:
        response = await client.post(
            "http://tailor-vm-ip:8000/tailor",
            headers={"X-Tailor-Secret": TAILOR_SECRET},
            files={"pdf_file": ("resume.pdf", f, "application/pdf")},
            data={"job_description": job_description_text},
        )
    result = response.json()
    pdf_bytes = base64.b64decode(result["pdf_base64"])
    match_score = result["analysis"]["overallScore"]
Applier receives the optimized PDF and match score, then applies to the job and calls the ResuMap webhook to log it:
POST https://resumap-app.replit.app/api/webhooks/application
X-Webhook-Secret: <webhook_secret>
{
  "userId": 42,
  "jobTitle": "Senior PM",
  "company": "Acme Corp",
  "matchScore": 78,
  "status": "Applied",
  "jobUrl": "https://..."
}
Troubleshooting
Error	Cause	Fix
OPENAI_API_KEY not found	Env var not set	Add to .env and restart
401 Invalid X-Tailor-Secret	Wrong secret	Make sure Scout/Applier use the same TAILOR_SECRET
400 This doesn't appear to be a resume	Uploaded wrong PDF	Check that the PDF has work experience, education, skills sections
400 This doesn't look like a job description	JD too short or generic	Paste the full job posting including responsibilities and requirements
500 AI failed to generate structured resume	OpenAI returned malformed JSON	Retry — this is transient
openai.RateLimitError	OpenAI quota exceeded	Check your OpenAI account usage/billing
PDF generation crashes	Missing font files	Ensure fonts/ folder is present with all Inter .ttf files
File structure
tailor-service/
├── server.py          # FastAPI app — /health, /extract, /tailor endpoints
├── tailor.py          # 3-call OpenAI optimization pipeline
├── extract.py         # PDF text extraction (pdfplumber + PyMuPDF)
├── generate_pdf.py    # PDF generation (ReportLab + Inter fonts)
├── fonts/             # Inter font family TTF files
│   ├── Inter-Regular.ttf
│   ├── Inter-Bold.ttf
│   ├── Inter-Italic.ttf
│   ├── Inter-BoldItalic.ttf
│   └── Inter-Medium.ttf
├── requirements.txt   # Python dependencies
├── .env.example       # Environment variable template
└── README.md          # This file