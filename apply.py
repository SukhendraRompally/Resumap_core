import os
import requests
import json
import asyncio
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()

# Get deployment name from environment
DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4o")

# Initialize Clients
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    api_version=os.getenv("AZURE_OPENAI_VERSION"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)
SKYVERN_API_KEY = os.getenv("SKYVERN_API_KEY")
SKYVERN_URL = "https://api.skyvern.com/api/v1"

def upload_resume_temporarily(file_path):
    """
    Uploads the PDF to a temporary host so Skyvern Cloud can download it.
    The file expires after 1 download or 14 days.
    """
    print(f"📤 Uploading {os.path.basename(file_path)} to temporary storage...")

    # Check if file exists
    if not os.path.exists(file_path):
        print(f"❌ File not found: {file_path}")
        return None

    try:
        with open(file_path, 'rb') as f:
            # Read file size for debugging
            f.seek(0, 2)  # Seek to end
            file_size = f.tell()
            f.seek(0)  # Seek back to start
            print(f"   File size: {file_size} bytes")

            response = requests.post(
                'https://catbox.moe/user/api.php',
                files={'fileToUpload': f},
                data={'reqtype': 'fileupload'},
                timeout=15
            )

            print(f"   Response status: {response.status_code}")

            if response.status_code == 200:
                link = response.text.strip()
                if link and link.startswith('https://'):
                    print(f"   ✅ Upload successful: {link}")
                    return link
                else:
                    print(f"⚠️ Invalid response from catbox.moe: {link}")
                    return None
            else:
                print(f"⚠️ Upload failed with status {response.status_code}")
                print(f"   Content-Type: {response.headers.get('content-type', 'unknown')}")
                print(f"   Response: {response.text[:500]}")
                return None
    except Exception as e:
        print(f"💥 File upload error: {e}")
        import traceback
        traceback.print_exc()
        return None

async def generate_gap_fill(field_description, user_profile, job_description):
    """
    LLM call to generate missing information based on the candidate's actual
    resume text and the specific job description.
    """
    # Use the extracted resume text we got from Replit/Storage
    resume_context = user_profile.get('resume_extracted_text', 'A high-achieving professional.')

    prompt = f"""
    You are an expert career strategist.
    CANDIDATE PROFILE: {resume_context[:2000]}
    JOB DESCRIPTION: {job_description[:2000]}

    TASK: The job application is asking for: "{field_description}".
    Generate a professional, high-conversion response.
    - If a cover letter, keep it under 300 words.
    - If a short question, keep it under 3 sentences.
    Return ONLY the text of the answer.
    """

    try:
        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[{"role": "system", "content": "You are a professional recruitment expert."},
                      {"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"⚠️ LLM Gap-Fill Failed for {field_description}: {e}")
        return "Please see attached resume for detailed experience."

async def trigger_skyvern_apply(job_url, pdf_path, user_profile, job_description):
    """
    Finalized Skyvern 2.0 dispatch script.
    Stitches together Replit data, the tailored PDF, and AI-generated content.
    """
    print(f"🛫 Starting application process for: {job_url}")

    # 1. Handle the Resume (Local -> Remote URL)
    public_resume_url = upload_resume_temporarily(pdf_path)
    if not public_resume_url:
        print("❌ File upload failed. This usually means:")
        print("   - file.io service is temporarily down")
        print("   - Network connectivity issue")
        print("   - File permissions issue")
        print(f"   - Check if file exists at: {pdf_path}")
        print("   - Consider checking file.io status or using alternative upload service")
        return None

    # also prepare base64 version in case Skyvern cannot trigger the file chooser
    try:
        with open(pdf_path, 'rb') as pf:
            import base64
            resume_b64 = base64.b64encode(pf.read()).decode('utf-8')
    except Exception:
        resume_b64 = None

    # 2. Check for missing cover letter/why us in the profile
    cover_letter = user_profile.get("cover_letter_text")
    if not cover_letter or len(cover_letter) < 50:
        print("✍️ Generating AI Cover Letter...")
        cover_letter = await generate_gap_fill("Full Cover Letter", user_profile, job_description)

    # 3. Construct the Skyvern 2.0 Payload
    payload = {
        "url": job_url,
        "prompt": (
            "Apply for this job. Navigate to the form, fill all fields including EEO "
            "and work authorization. For the resume field, first try uploading from the URL "
            f"{public_resume_url}. If that fails (button never opens file chooser), use the "
            "provided base64 string (resume_base64) as the file. Once resume is attached, "
            "submit the form."
        ),
        "navigation_goal": (
            "Complete the job application. Use the following logic: "
            "1. If a DataDome or Captcha appears, use the SOLVE_CAPTCHA action immediately. "
            "2. For the resume, prioritize URL then base64. "
            "3. If asked for a password to create an account, use 'ResuMap2026!'. "
            "4. Do not stop until you see a 'Thank you' or 'Application Submitted' message."
        ),
        "engine": "skyvern-2.0",
        "proxy_location": "RESIDENTIAL",
        "navigation_payload": {
            # Identity (Mapped from Replit)
            "first_name": user_profile.get("first_name"),
            "last_name": user_profile.get("last_name"),
            "email": user_profile.get("email"),
            "phone_number": user_profile.get("phone"),

            # Address
            "city": user_profile.get("city"),
            "state": user_profile.get("state"),
            "zip_code": user_profile.get("zip_code"),

            # Professional / Social
            "linkedin_url": user_profile.get("linkedin_url"),
            "github_url": user_profile.get("github_url"),
            "website_url": user_profile.get("portfolio_url", ""),

            # Legal & Logistics
            "work_authorization_status": user_profile.get("work_authorization"),
            "requires_sponsorship": user_profile.get("requires_sponsorship"),
            "notice_period": user_profile.get("notice_period", "Immediate"),
            "education_level": user_profile.get("highest_education"),
            "referral_source": user_profile.get("how_did_you_hear", "LinkedIn"),

            # EEO / Voluntary Disclosures
            "gender": user_profile.get("gender", "Decline to Self-Identify"),
            "race": user_profile.get("race", "Decline to Self-Identify"),
            "veteran_status": user_profile.get("veteran_status", "Decline to Self-Identify"),
            "disability_status": user_profile.get("disability_status", "Decline to Self-Identify"),

            # The Assets (URL instead of Path for Cloud stability). Include base64 as backup.
            "resume_url": public_resume_url,
            "resume_base64": resume_b64,
            "cover_letter": cover_letter,

            # Dynamic Answers (For custom form questions)
            "why_this_company": await generate_gap_fill("Why do you want to work at this company?", user_profile, job_description),
            "salary_expectations": user_profile.get("desired_salary", "Competitive/Market Rate")
        }
    }

    headers = {
        "x-api-key": SKYVERN_API_KEY,
        "Content-Type": "application/json"
    }

    # 4. Final Dispatch
    try:
        response = requests.post(
            f"{SKYVERN_URL}/tasks",
            json=payload,
            headers=headers,
            timeout=30
        )

        # Skyvern returns 201 for new tasks but some deployments currently
        # respond with 200 and a `task_id` field instead. Treat both as success.
        if response.status_code in (200, 201):
            data = response.json()
            run_id = data.get('run_id') or data.get('task_id')
            print(f"✅ Skyvern Mission Launched! Run ID: {run_id} (status {response.status_code})")
            return run_id
        else:
            print(f"❌ Skyvern API rejection ({response.status_code}): {response.text}")
            return None

    except Exception as e:
        print(f"💥 Skyvern dispatch error: {e}")
        import traceback
        traceback.print_exc()
        return None