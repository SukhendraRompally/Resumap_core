import os
from dotenv import load_dotenv
import requests
import json
from bs4 import BeautifulSoup
from openai import AzureOpenAI
import time
import tailor
import apply
import generate_pdf
import subprocess
import asyncio
import re

# Load the keys from your .env file
load_dotenv()
REPLIT = os.getenv("REPLIT_BASE_URL")  # Base URL for Replit

# Webhook configuration
REPLIT_URL = os.getenv("REPLIT_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
W_HEADERS = {
    "X-Webhook-Secret": WEBHOOK_SECRET,
    "Content-Type": "application/json"
}

# Azure OpenAI configuration
client = AzureOpenAI(
    api_key=os.getenv("AZURE_OPENAI_KEY"),
    api_version=os.getenv("AZURE_OPENAI_VERSION"),
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT")
)
DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

# ===== INTELLIGENCE LAYER: JOB QUALITY FILTERING =====

def get_actual_destination(adzuna_url):
    """Follow redirects to get the real job posting URL"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        # Use stream=True to avoid downloading the full page content
        with requests.get(adzuna_url, headers=headers, allow_redirects=True, timeout=8, stream=True) as r:
            return r.url.lower()
    except Exception as e:
        return adzuna_url.lower()


# ===== LAYER 1: JOB DISCOVERY =====

def fetch_jobs(profile):
    """Fetch jobs from Adzuna API with smart filtering"""
    # 1. Pull dynamic values from the profile
    target_role = profile.get("target_role") or "Product Manager"
    target_location = profile.get("target_location") or "San Francisco"

    # 2. Clean and encode the search terms exactly like a browser
    from urllib.parse import quote
    what = quote(target_role)
    where = quote(target_location)

    # 3. Use profile keys
    app_id = os.getenv("ADZUNA_APP_ID").strip()
    app_key = os.getenv("ADZUNA_APP_KEY").strip()

    # 4. Build the URL
    url = (
        f"https://api.adzuna.com/v1/api/jobs/us/search/1?"
        f"app_id={app_id}&app_key={app_key}"
        f"&what={what}&where={where}"
        f"&full_time=1&results_per_page=50&max_days_old=30"
    )

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        print(f"📡 Searching for: {target_role} in {target_location}")
        response = requests.get(url, headers=headers, timeout=15)
        print(f"🔗 TEST THIS LINK: {response.url}")

        if response.status_code == 200:
            data = response.json()
            raw_results = data.get('results', [])
            print(f"🕵️ Deep-sniffing {len(raw_results)} leads for hidden aggregators...")

            filtered_results = []
            # Define quality filters
            blacklisted_domains = ["ivyexec", "ivy-exec", "linkedin", "myworkday", "recruit.net", "lensa"]
            gold_standard = ["greenhouse.io", "lever.co", "ashbyhq", "workable", "breezy.hr"]

            for job in raw_results:
                # Extract job details
                title = job.get('title', 'Unknown Title')
                company_info = job.get('company', {})
                company_name = company_info.get('display_name', '').lower()
                redirect_url = job.get('redirect_url', '').lower()

                # Layer 1: Meta filter
                is_junk_meta = any(term in company_name or term in redirect_url for term in blacklisted_domains)

                # Layer 2: Sniffer - follow redirect to get real URL
                final_url = get_actual_destination(redirect_url)
                is_junk_sniff = any(term in final_url for term in blacklisted_domains)

                # Debug logging
                if is_junk_meta or is_junk_sniff:
                    print(f"❌ KILLED: {title} | Reason: {'Meta' if is_junk_meta else 'Sniffer'} | Final Dest: {final_url[:50]}...")
                    continue
                else:
                    print(f"🟢 KEEPING: {title} | Company: {company_name} | Dest: {final_url[:50]}...")

                # Tag and save
                job['is_gold'] = any(term in final_url for term in gold_standard)
                job['clean_url'] = final_url

                filtered_results.append(job)

            # Sort so the best jobs (Greenhouse/Lever) are processed first
            filtered_results.sort(key=lambda x: x.get('is_gold', False), reverse=True)

            print(f"✅ Success! Found {len(filtered_results)} high-quality leads.")
            return filtered_results
        else:
            print(f"❌ Error {response.status_code}: {response.text}")
            return []
    except Exception as e:
        print(f"❗ Failed to connect: {e}")
        return []


# ===== LAYER 2: FULL JOB DESCRIPTION EXTRACTION =====

from playwright_stealth import Stealth
from playwright.sync_api import sync_playwright

def get_full_job_description(url):
    """Extract full job description using stealth Playwright"""
    print(f"🌐 Stealth Browser launching for: {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-accelerated-2d-canvas',
                    '--no-first-run',
                    '--no-zygote',
                    '--disable-gpu'
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={'width': 1920, 'height': 1080}
            )
            
            # Apply stealth mode
            stealth = Stealth()
            stealth.apply_stealth_sync(context)
            
            page = context.new_page()

            # Navigate and wait for network to settle
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            content = page.content()
            soup = BeautifulSoup(content, 'html.parser')

            # Remove noise
            for noise in soup(["script", "style", "iframe"]):
                noise.extract()

            text = soup.get_text(separator=' ')
            clean_text = ' '.join(text.split())

            browser.close()
            return clean_text[:4000]
    except Exception as e:
        print(f"⚠️ Scrape error: {str(e)}")
        return f"Scrape failed: {str(e)}"


# ===== LAYER 3: AI MATCHING ENGINE =====

def get_ai_score(full_description, user_data):
    """Score job match using Azure OpenAI"""
    # Handle both string (full resume) and dict (profile) formats
    if isinstance(user_data, str):
        user_context = f"FULL RESUME TEXT:\n{user_data}"
    else:
        user_context = f"PROFILE SUMMARY: {user_data.get('summary')}\nMUST-HAVES: {user_data.get('must_haves')}"

    prompt = f"""
You are an expert technical recruiter analyzing job-candidate fit.

CANDIDATE DATA:
{user_context}

JOB DESCRIPTION:
{full_description}

TASK:
Assign a Match Score (0-100) based on how well the candidate matches the role requirements.
Consider years of experience, required skills, job type, and location preference.
Provide a brief 1-sentence reason.

Return ONLY a JSON object with this exact format:
{{"score": 85, "reason": "User has 3 years of RAG experience which matches the JD requirement."}}
"""

    try:
        response = client.chat.completions.create(
            model=DEPLOYMENT_NAME,
            messages=[
                {"role": "system", "content": "You are a recruitment AI that outputs JSON only."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        print(f"⚠️ AI Scoring Error: {e}")
        return {"score": 0, "reason": "Error during analysis"}


# ===== LAYER 4: AUTOMATION PIPELINE =====

def run_automation_pipeline(extracted_text, user_profile, user_id, final_rankings=None):
    """
    Main automation pipeline: Score jobs → Tailor resumes → Generate PDFs → Apply via Skyvern
    
    Args:
        extracted_text: User's resume text
        user_profile: User profile dict with target_role, target_location, must_haves, etc.
        user_id: Unique user identifier
        final_rankings: Pre-scored job rankings (if from main execution)
    """
    
    print(f"🚀 Resumap Scout starting for {user_profile.get('target_role')} (User: {user_id})...")

    # Create resumes directory if it doesn't exist
    resumes_dir = "resumes"
    os.makedirs(resumes_dir, exist_ok=True)
    
    # Create applications directory for manifests
    applications_dir = f"applications/{user_id}"
    os.makedirs(applications_dir, exist_ok=True)

    if final_rankings is not None:
        # Use the pre-scored jobs from direct execution
        top_jobs = sorted(final_rankings, key=lambda x: x['score'], reverse=True)[:10]
        print(f"📊 Using {len(top_jobs)} pre-scored job matches")
    else:
        # Fetch and score jobs when called via API
        job_list = fetch_jobs(user_profile)
        print(f"Found {len(job_list)} potential leads for User {user_id}.")

        scored_jobs = []
        
        # Step 2: Score ALL jobs first (using API descriptions only - no scraping)
        for job in job_list:
            company_name = job['company'].get('display_name') or "Unknown_Company"
            job_title = job.get('title') or "Role"

            print(f"🔍 Scoring: {job_title} @ {company_name}...")

            # Use ONLY API description for fast scoring (no scraping fallback)
            full_description = job.get('description', 'No description available')
            
            analysis = get_ai_score(full_description, extracted_text)
            score = analysis.get('score', 0)
            reason = analysis.get('reason', '')

            scored_jobs.append({
                "score": score,
                "reason": reason,
                "job": job,
                "description": full_description  # API description for now
            })

        # Apply domain bonuses/penalties
        gold_standard = ["greenhouse.io", "lever.co", "ashbyhq", "workable", "breezy.hr"]
        blacklisted_domains = ["ivyexec", "ivy-exec", "linkedin", "myworkday", "recruit.net", "lensa"]
        
        for item in scored_jobs:
            job = item['job']
            final_url = job.get('clean_url', job.get('redirect_url', ''))
            bonus = 0
            if any(term in final_url for term in gold_standard):
                bonus += 10
                print(f"🏆 Gold standard bonus +10 for {job.get('title')}")
            if any(term in final_url for term in blacklisted_domains):
                bonus -= 10
                print(f"⚠️ Blacklist penalty -10 for {job.get('title')}")
            item['weighted_score'] = item['score'] + bonus

        # Sort all by weighted score and pick top 10
        scored_jobs.sort(key=lambda x: x['weighted_score'], reverse=True)
        top_jobs = scored_jobs[:3]
        
        print(f"🏆 Selected {len(top_jobs)} top matches after weighting")
        for i, item in enumerate(top_jobs[:3]):  # Show top 5
            print(f"  {i+1}. {item['job'].get('title')} @ {item['job']['company'].get('display_name')} - {item['weighted_score']}% (base: {item['score']}%)")

    final_manifest_list = []

    # Step 3: Tailor and Generate
    for item in top_jobs:
        job = item['job']
        score = item['weighted_score']  # Use weighted score
        description = item['description']
        
        # For top matches, get full description if API one is short
        if len(description) < 500:
            print(f"📄 Getting full description for top match...")
            scraped_desc = get_full_job_description(job.get('clean_url', job['redirect_url']))
            if scraped_desc and len(scraped_desc) > len(description):
                description = scraped_desc
                print(f"✅ Enhanced description: {len(description)} chars")
        
        company = job.get('company', {}).get('display_name', 'Company')
        job_title = job.get('title', 'Role')
        clean_company = re.sub(r'\W+', '', company)
        clean_title = re.sub(r'\W+', '', job_title)
        
        # Naming convention: USERID_CompanyName_JobTitle.pdf
        filename = f"{user_id}_{clean_company}_{clean_title}.pdf"
        file_path = os.path.join(resumes_dir, filename)
        
        print(f"🎯 Tailoring Top Match: {company} ({score}%)")
        
        try:
            # Generate tailored resume using async tailor_resume
            tailored_result = asyncio.run(tailor.tailor_resume(extracted_text, description))
            structured_resume = tailored_result['structured_resume']
            
            # Generate PDF from structured resume
            generate_pdf.generate_pdf(file_path, structured_resume)
            print(f"✅ Saved: {file_path}")
            
            # Create manifest in applications folder
            manifest_filename = f"{user_id}_{clean_company}_{clean_title}.json"
            manifest_path = os.path.join(applications_dir, manifest_filename)
            manifest_data = {
                "USER_ID": user_id,
                "job_id": job.get('id'),
                "title": job.get('title'),
                "company": company,
                "url": job.get('redirect_url'),
                "resume_path": file_path,
                "score": score,
                "reason": item.get('reason', '')
            }
            
            with open(manifest_path, "w") as f:
                json.dump(manifest_data, f, indent=2)

            final_manifest_list.append(manifest_data)
            print(f"✅ Success: Tailored and Created Manifest for {job.get('title')}")
            
            # Trigger Skyvern apply for this job
            try:
                asyncio.run(apply.trigger_skyvern_apply(
                    job_url=job.get('redirect_url'),
                    pdf_path=file_path,
                    user_profile=user_profile,
                    job_description=description
                ))
                print(f"🚀 Skyvern apply triggered for {job.get('title')}")
            except Exception as e:
                print(f"⚠️ Skyvern apply warning for {job.get('title')}: {e}")
            
        except Exception as e:
            print(f"❌ Tailoring failed for {job.get('title')}: {e}")
            import traceback
            traceback.print_exc()

    # Return the list of successfully prepared applications
    return final_manifest_list


# ===== MAIN EXECUTION =====

def fetch_user_data_from_replit():
    """
    Fetch real user data from Replit API.
    Returns a list of (extracted_text, user_profile, user_id) tuples.
    """
    try:
        print("🔗 Fetching active users from Replit...")
        response = requests.get(
            f"{REPLIT_URL}/api/webhooks/users/active",  # ← correct endpoint
            headers=W_HEADERS,
            timeout=10
        )
        response.raise_for_status()
        users = response.json()  # ← this is a LIST, not a dict
        print(f"✅ Found {len(users)} active user(s).")
        results = []
        for user in users:
            user_id      = user.get('id')
            user_profile = user.get('profile')
            resume_text  = user.get('resume_text')
            if not resume_text:
                print(f"⚠️ Skipping user {user_id}: no resume uploaded yet.")
                continue
            if user.get('dailyCapRemaining', 0) == 0:
                print(f"⚠️ Skipping user {user_id}: daily cap reached.")
                continue
            results.append((resume_text, user_profile, user_id))
        return results
    except Exception as e:
        print(f"⚠️ Failed to fetch from Replit ({REPLIT_URL}): {e}")
        return []
if __name__ == "__main__":
    print("🚀 Resumap Scout starting...")
    users = fetch_user_data_from_replit()
    for extracted_text, user_profile, user_id in users:
        results = run_automation_pipeline(extracted_text, user_profile, user_id)
        print(f"✅ User {user_id}: {len(results)} applications generated.")
