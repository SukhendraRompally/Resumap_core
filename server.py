import os
import re
import base64
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from extract import extract_text_from_pdf
from generate_pdf import generate_pdf
from tailor import tailor_resume

app = FastAPI(title="Tailor Service", version="1.0.0")

TAILOR_SECRET = os.environ.get("TAILOR_SECRET", "")


def _check_auth(x_tailor_secret: str | None) -> None:
    if not TAILOR_SECRET:
        return
    if x_tailor_secret != TAILOR_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Tailor-Secret header")


RESUME_SECTION_HEADERS = [
    re.compile(r"^[\s]*(?:professional\s+)?experience\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*education\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*(?:technical\s+)?skills\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*work\s*history\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*professional\s+summary\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*(?:employment|employment\s+history)\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*resume\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*curriculum\s*vitae\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*summary\s+of\s+qualifications\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*career\s+objective\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*certifications?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*publications?\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[\s]*projects?\s*$", re.IGNORECASE | re.MULTILINE),
]

CAPS_HEADERS = [
    re.compile(r"\bEXPERIENCE\b"),
    re.compile(r"\bEDUCATION\b"),
    re.compile(r"\bSKILLS\b"),
    re.compile(r"\bEMPLOYMENT\b"),
    re.compile(r"\bCERTIFICATIONS?\b"),
    re.compile(r"\bPUBLICATIONS?\b"),
    re.compile(r"\bPROJECTS?\b"),
    re.compile(r"\bSUMMARY\b"),
]

CONTACT_SIGNALS = [
    re.compile(r"linkedin\.com\/in\/", re.IGNORECASE),
    re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!.*goindigo|.*airline|.*booking)[A-Za-z0-9.-]+\.[A-Z]{2,}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\(\d{3}\)\s*\d{3}[\s-]?\d{4}"),
]

DATE_RANGE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|"
    r"June|July|August|September|October|November|December)\s*[\'']?\d{2,4}\s*[-–—]\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|"
    r"June|July|August|September|October|November|December|Present|Current)\b",
    re.IGNORECASE,
)


def _validate_resume(text: str) -> str | None:
    """Return an error message string if text is not a resume, else None."""
    has_bullet_points = len(re.findall(r"^[\s]*[●•]\s", text, re.MULTILINE)) >= 3
    has_date_ranges = bool(DATE_RANGE_RE.search(text))
    has_all_caps_headers = len(re.findall(r"^[A-Z][A-Z\s&]+$", text, re.MULTILINE)) >= 2

    section_count = sum(1 for r in RESUME_SECTION_HEADERS if r.search(text))
    caps_count = sum(1 for r in CAPS_HEADERS if r.search(text))
    contact_count = sum(1 for r in CONTACT_SIGNALS if r.search(text))
    structural_score = (
        (2 if has_bullet_points else 0)
        + (3 if has_date_ranges else 0)
        + (2 if has_all_caps_headers else 0)
    )
    total_score = section_count * 2 + caps_count + contact_count + structural_score

    print(
        f"Resume validation — lineHeaders: {section_count}, capsHeaders: {caps_count}, "
        f"contacts: {contact_count}, bullets: {has_bullet_points}, dates: {has_date_ranges}, "
        f"allCapsLines: {has_all_caps_headers}, score: {total_score}"
    )

    if total_score < 6:
        return (
            "This doesn't appear to be a resume. Please upload a PDF that contains "
            "your work experience, education, and skills."
        )
    return None


JD_STRONG_SIGNALS = [
    re.compile(r"\bresponsibilit(y|ies)\b", re.IGNORECASE),
    re.compile(r"\brequirement(s)?\b", re.IGNORECASE),
    re.compile(r"\bqualification(s)?\b", re.IGNORECASE),
    re.compile(r"\bcandidate(s)?\b", re.IGNORECASE),
    re.compile(r"\bapplicant(s)?\b", re.IGNORECASE),
    re.compile(r"\byears?\s+(of\s+)?experience\b", re.IGNORECASE),
    re.compile(r"\bjob\s+description\b", re.IGNORECASE),
    re.compile(r"\bfull[\s-]?time\b", re.IGNORECASE),
    re.compile(r"\bpart[\s-]?time\b", re.IGNORECASE),
    re.compile(r"\bsalary\b", re.IGNORECASE),
    re.compile(r"\bcompensation\b", re.IGNORECASE),
    re.compile(r"\bbenefits\b", re.IGNORECASE),
    re.compile(r"\bapply\b", re.IGNORECASE),
    re.compile(r"\bhiring\b", re.IGNORECASE),
    re.compile(r"\brecruiting\b", re.IGNORECASE),
    re.compile(r"\brole\s+(overview|summary|description)\b", re.IGNORECASE),
    re.compile(r"\bwho\s+you\s+are\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+you('ll| will)\b", re.IGNORECASE),
    re.compile(r"\bmust[\s-]have\b", re.IGNORECASE),
    re.compile(r"\bnice[\s-]to[\s-]have\b", re.IGNORECASE),
    re.compile(r"\bpreferred\s+qualifications?\b", re.IGNORECASE),
    re.compile(r"\brequired\s+skills?\b", re.IGNORECASE),
]

JD_MEDIUM_SIGNALS = [
    re.compile(r"\bexperience\b", re.IGNORECASE),
    re.compile(r"\bskills?\b", re.IGNORECASE),
    re.compile(r"\brole\b", re.IGNORECASE),
    re.compile(r"\bposition\b", re.IGNORECASE),
    re.compile(r"\bcollaborate\b", re.IGNORECASE),
    re.compile(r"\bstakeholder", re.IGNORECASE),
    re.compile(r"\bcross[\s-]?functional\b", re.IGNORECASE),
    re.compile(r"\bproficiency\b", re.IGNORECASE),
    re.compile(r"\bproficient\b", re.IGNORECASE),
    re.compile(r"\bbachelor", re.IGNORECASE),
    re.compile(r"\bmaster", re.IGNORECASE),
    re.compile(r"\bdegree\b", re.IGNORECASE),
    re.compile(r"\bremote\b", re.IGNORECASE),
    re.compile(r"\bhybrid\b", re.IGNORECASE),
    re.compile(r"\bonsite\b", re.IGNORECASE),
    re.compile(r"\bon[\s-]?site\b", re.IGNORECASE),
]


def _validate_job_description(jd: str) -> str | None:
    """Return an error message string if JD is invalid, else None."""
    trimmed = jd.strip()
    if len(trimmed) < 50:
        return (
            "The job description is too short. Please paste a real job posting "
            "with role details, requirements, and responsibilities."
        )
    strong_count = sum(1 for r in JD_STRONG_SIGNALS if r.search(trimmed))
    medium_count = sum(1 for r in JD_MEDIUM_SIGNALS if r.search(trimmed))
    jd_score = strong_count * 2 + medium_count
    if jd_score < 3:
        return (
            "This doesn't look like a job description. Please paste a real job posting "
            "that includes the role, responsibilities, and requirements."
        )
    return None


@app.get("/health")
async def health():
    return {"status": "ok", "service": "tailor"}


@app.post("/extract")
async def extract(
    pdf_file: UploadFile = File(...),
    x_tailor_secret: str | None = Header(default=None),
):
    _check_auth(x_tailor_secret)

    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    tmp_path = None
    try:
        suffix = ".pdf"
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)

        content = await pdf_file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        result = extract_text_from_pdf(tmp_path)
        if "error" in result:
            raise HTTPException(status_code=400, detail=f"PDF extraction failed: {result['error']}")
        if not result.get("text") or not result["text"].strip():
            raise HTTPException(status_code=400, detail="Could not extract any text from the PDF. Please ensure the PDF contains readable text.")

        return JSONResponse({"text": result["text"], "layout": result.get("layout", [])})

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


@app.post("/tailor")
async def tailor(
    pdf_file: UploadFile = File(...),
    job_description: str = Form(...),
    x_tailor_secret: str | None = Header(default=None),
):
    _check_auth(x_tailor_secret)

    if not pdf_file.filename or not pdf_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    jd_error = _validate_job_description(job_description)
    if jd_error:
        raise HTTPException(status_code=400, detail=jd_error)

    tmp_pdf = None
    tmp_out = None
    try:
        tmp_fd, tmp_pdf = tempfile.mkstemp(suffix=".pdf")
        os.close(tmp_fd)

        content = await pdf_file.read()
        with open(tmp_pdf, "wb") as f:
            f.write(content)

        extract_result = extract_text_from_pdf(tmp_pdf)
        if "error" in extract_result:
            raise HTTPException(status_code=400, detail=f"PDF extraction failed: {extract_result['error']}")

        extracted_text = extract_result.get("text", "")
        if not extracted_text.strip():
            raise HTTPException(
                status_code=400,
                detail="Could not extract any text from the PDF. Please ensure the PDF contains readable text.",
            )

        resume_error = _validate_resume(extracted_text)
        if resume_error:
            raise HTTPException(status_code=400, detail=resume_error)

        tailor_result = await tailor_resume(extracted_text, job_description)

        structured_resume = tailor_result["structured_resume"]
        if not structured_resume.get("name") or not structured_resume.get("sections"):
            raise HTTPException(
                status_code=500,
                detail="AI failed to generate structured resume data. Please try again.",
            )

        tmp_fd2, tmp_out = tempfile.mkstemp(suffix=".pdf")
        os.close(tmp_fd2)

        generate_pdf(tmp_out, structured_resume)

        with open(tmp_out, "rb") as f:
            pdf_bytes = f.read()

        pdf_base64 = base64.b64encode(pdf_bytes).decode("utf-8")

        original_name = Path(pdf_file.filename).stem
        output_filename = f"{original_name}_optimized.pdf"

        return JSONResponse({
            "pdf_base64": pdf_base64,
            "filename": output_filename,
            "analysis": tailor_result["analysis"],
        })

    except HTTPException:
        raise
    except Exception as e:
        print(f"Tailor error: {e}")
        raise HTTPException(status_code=500, detail=f"Optimization failed: {str(e)}")
    finally:
        for p in [tmp_pdf, tmp_out]:
            if p and os.path.exists(p):
                os.remove(p)
