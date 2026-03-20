import os
import re
import asyncio
import time
import json
import traceback
import shutil
import subprocess
from datetime import datetime, timedelta
from dotenv import load_dotenv
import openai

# Essential Stealth & Stagehand Imports
from playwright.async_api import async_playwright
from playwright_stealth import stealth
from stagehand import AsyncStagehand

load_dotenv()

def sanitize_profile(profile: dict) -> dict:
    """Normalize profile values to strings so Stagehand validation is satisfied."""
    sanitized = {}
    for k, v in (profile or {}).items():
        if v is None:
            sanitized[k] = ""
        elif isinstance(v, (list, tuple)):
            sanitized[k] = ", ".join(map(str, v))
        else:
            sanitized[k] = str(v)
    return sanitized

def _llm_gap_fill(api_key: str, model: str, prompt: str) -> str:
    """Call OpenAI synchronously to pre-generate a missing field value."""
    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.7,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return ""


def _pre_generate_answers(profile: dict, resume_text: str, company_name: str,
                           job_title: str, api_key: str, model: str) -> dict:
    """
    Pre-generate LLM answers for fields that are missing or None in the profile.
    Returns a dict of field_name → generated_value to merge into the profile.
    """
    extras = {}
    resume_snippet = (resume_text or "")[:3000]

    # Summary / professional bio
    if not profile.get("summary") and not profile.get("headline"):
        extras["generated_summary"] = _llm_gap_fill(api_key, model,
            f"Write a 2-sentence professional summary for this candidate based on their resume:\n{resume_snippet}"
        )

    # Salary — use minimumSalary if set, else generate
    min_sal = profile.get("minimumSalary") or profile.get("minimum_salary") or ""
    if not min_sal:
        extras["generated_salary"] = _llm_gap_fill(api_key, model,
            f"What is a competitive annual salary (USD) for a {job_title} role at {company_name}? "
            f"Give a single dollar figure at the upper-mid market range. Respond with only the number, e.g. 185000"
        ) or "160000"

    # Why this company
    extras["generated_motivation"] = _llm_gap_fill(api_key, model,
        f"Write 2-3 enthusiastic sentences explaining why this candidate wants to work at {company_name} "
        f"as a {job_title}. Base it on their background:\n{resume_snippet[:1500]}\n"
        f"Be specific, positive, and make it sound genuinely motivated. Do not use clichés like 'I am passionate'."
    )

    # Cover letter (short)
    extras["generated_cover_letter"] = _llm_gap_fill(api_key, model,
        f"Write a 3-sentence cover letter opening for {profile.get('full_name', profile.get('first_name', 'the candidate'))} "
        f"applying for {job_title} at {company_name}. "
        f"Highlight their strongest relevant experience from this resume:\n{resume_snippet[:1500]}\n"
        f"Be confident and concrete. No generic filler."
    )

    # Skills summary
    if not profile.get("skills"):
        extras["generated_skills"] = _llm_gap_fill(api_key, model,
            f"List the top 8 technical and product skills of this candidate as a comma-separated list:\n{resume_snippet}"
        )

    print(f"✅ Pre-generated {len(extras)} gap-fill answers for {company_name}")
    return extras


async def _read_verification_code_from_email(
    email_address: str,
    app_password: str,
    imap_host: str = "imap.gmail.com",
    timeout_secs: int = 60,
    min_timestamp: float = 0,
) -> str | None:
    """
    Poll IMAP inbox for a verification code email sent in the last 10 minutes.
    Handles both numeric (123456) and alphanumeric (SBBPROXa) codes.
    Greenhouse sends alphanumeric codes in an <h1> tag in HTML-only emails.
    Returns the code string, or None on timeout.
    """
    import imaplib
    import email as _email_lib
    import re

    def _extract_code(msg) -> str | None:
        """Extract verification code from email message (handles HTML + text/plain)."""
        html_body = ""
        text_body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                raw = part.get_payload(decode=True)
                if not raw:
                    continue
                decoded = raw.decode(errors="ignore")
                if ct == "text/plain":
                    text_body = decoded
                elif ct == "text/html":
                    html_body = decoded
        else:
            raw = msg.get_payload(decode=True)
            decoded = (raw or b"").decode(errors="ignore")
            if "<html" in decoded.lower():
                html_body = decoded
            else:
                text_body = decoded

        # 1. Greenhouse-style: code is in <h1> tag in HTML email
        if html_body:
            m = re.search(r'<h[1-3][^>]*>\s*([A-Za-z0-9]{4,12})\s*</h[1-3]>', html_body, re.IGNORECASE)
            if m:
                return m.group(1).strip()
            # 2. "paste this code" context — extract next token
            m = re.search(r'(?:paste|enter|copy)[^<]{0,80}(?:code|field)[^<]{0,40}<[^>]+>\s*([A-Za-z0-9]{4,12})\s*<', html_body, re.IGNORECASE | re.DOTALL)
            if m:
                return m.group(1).strip()
            # 3. Any numeric 6-digit code in stripped HTML
            text_from_html = re.sub(r'<[^>]+>', ' ', html_body)
            m = re.search(r'\b(\d{6})\b', text_from_html)
            if m:
                return m.group(1)

        # 4. Plain text fallback: numeric code
        if text_body:
            m = re.search(r'\b(\d{6})\b', text_body)
            if m:
                return m.group(1)

        return None

    deadline = time.time() + timeout_secs
    since_date = (datetime.utcnow() - timedelta(days=2)).strftime("%d-%b-%Y")

    # Only accept emails received after this timestamp
    # min_timestamp should be set to just before the submit button was clicked
    fresh_cutoff = min_timestamp if min_timestamp > 0 else (time.time() - 300)

    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL(imap_host)
            mail.login(email_address, app_password)
            # Search inbox AND all mail (in case Gmail routes to a label)
            for folder in ("inbox", '"[Gmail]/All Mail"'):
                try:
                    mail.select(folder)
                except Exception:
                    continue
                _, ids = mail.search(None, f'SINCE "{since_date}"')
                for mid in reversed(ids[0].split() or []):  # newest first
                    _, data = mail.fetch(mid, "(RFC822)")
                    msg = _email_lib.message_from_bytes(data[0][1])
                    frm = msg.get("From", "").lower()
                    subj = msg.get("Subject", "").lower()
                    # Only process emails that look like verification code emails
                    if not any(kw in frm + subj for kw in [
                        "greenhouse", "workday", "lever", "ashby", "code", "verify", "security"
                    ]):
                        continue
                    # Skip stale emails from previous application attempts
                    try:
                        from email.utils import parsedate_to_datetime as _pdt
                        msg_ts = _pdt(msg.get("Date", "")).timestamp()
                        if msg_ts < fresh_cutoff:
                            continue
                    except Exception:
                        pass  # if date parse fails, proceed anyway
                    code = _extract_code(msg)
                    if code:
                        mail.logout()
                        print(f"  📧 Verification code found: {code}")
                        return code
            mail.logout()
        except Exception as e:
            print(f"  ⚠️ IMAP check error: {e}")

        await asyncio.sleep(5)

    return None


async def _fill_react_selects_native(page, api_key: str, model_name: str, profile: dict, company: str, salary: str) -> None:
    """Find all unselected React-Select dropdowns, use a single LLM call to pick answers, click natively."""
    try:
        rs_all = await page.query_selector_all(
            '[class*="react-select__container"], [class*="select__container"]'
        )
        rs_fields = []
        for rsc in rs_all:
            ph = await rsc.query_selector('[class*="__placeholder"]')
            if not ph:
                continue
            if (await ph.text_content() or "").strip().lower() not in ("select...", "select", ""):
                continue
            lbl = await page.evaluate("""(el) => {
                let node = el;
                for (let i = 0; i < 10; i++) {
                    node = node.parentElement;
                    if (!node) return '';
                    const lbl = node.querySelector('label, [class*="label"]');
                    if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
                }
                return '';
            }""", rsc)
            if not lbl:
                continue
            ctrl = await rsc.query_selector('[class*="__control"]')
            if not ctrl:
                continue
            await ctrl.click()
            await asyncio.sleep(0.5)
            opt_els = await page.query_selector_all('[class*="__option"]:not([class*="disabled"])')
            opts = []
            for oe in opt_els:
                ot = (await oe.text_content() or "").strip()
                if ot:
                    opts.append(ot)
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            if opts:
                rs_fields.append({"label": lbl, "options": opts, "container": rsc})

        if not rs_fields:
            return

        rs_profile = {
            "homeCity": profile.get("city") or "Mountain View",
            "homeState": profile.get("state") or "California",
            "workAuthorization": "Authorized to work in the US, no sponsorship required",
            "educationLevel": profile.get("educationLevel") or profile.get("highest_education") or "Bachelor's Degree",
            "yearsExperience": str(profile.get("yearsExperience") or ""),
            "salaryExpectation": salary,
            "currentCompany": profile.get("currentCompany") or profile.get("current_company") or "",
            "gender": profile.get("gender") or "Male",
            "race": profile.get("raceEthnicity") or profile.get("race") or "Asian",
            "veteranStatus": profile.get("veteranStatus") or profile.get("veteran_status") or "Not a protected veteran",
            "disabilityStatus": profile.get("disabilityStatus") or profile.get("disability_status") or "No disability",
        }
        profile_text = "\n".join(f"  {k}: {v}" for k, v in rs_profile.items() if v)
        questions = "\n".join(
            f'{i+1}. Field: "{f["label"]}"\n   Options: {f["options"]}'
            for i, f in enumerate(rs_fields)
        )
        prompt = (
            f"You are filling a job application at {company}. "
            f"Applicant profile:\n{profile_text}\n\n"
            f"For each dropdown, pick the single best matching option (exact text from the list).\n"
            f"Rules:\n"
            f"- Work authorization → pick the 'authorized / yes / no sponsorship' option\n"
            f"- Visa sponsorship required → No\n"
            f"- Office/work location (which city will you work from) → pick whichever city is listed\n"
            f"- Where do you live/reside → use homeCity or homeState\n"
            f"- Bay Area / willing to work in office / hybrid / onsite → Yes\n"
            f"- Relocation → Yes\n"
            f"- Currently employed at {company} / previous employee → No\n"
            f"- Start date → Immediately\n"
            f"- Gender / identify my gender → use gender from profile\n"
            f"- Race / ethnicity / identify my race → use race from profile, pick closest available option\n"
            f"- Veteran status → use veteranStatus from profile, pick closest available option\n"
            f"- Disability / physical disability → use disabilityStatus from profile, pick closest option\n"
            f"- Hispanic / Latino → No\n"
            f"- Any yes/no about qualifications → Yes\n"
            f"- Any consent / acknowledgement → Yes or Acknowledged\n"
            f"- Any other → most qualified/positive option\n\n"
            f"Fields:\n{questions}\n\n"
            f"Reply with ONLY a numbered list, one answer per line (exact option text):\n"
            f"1. [answer]\n2. [answer]\netc."
        )
        oai_client = openai.OpenAI(api_key=api_key)
        resp = oai_client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0,
        )
        answers = {}
        for aline in resp.choices[0].message.content.strip().splitlines():
            m = re.match(r"^\s*(\d+)\.\s*(.+)$", aline.strip())
            if m:
                idx, val = int(m.group(1)) - 1, m.group(2).strip().strip('"').strip("'")
                if 0 <= idx < len(rs_fields):
                    answers[idx] = val

        for fi, field in enumerate(rs_fields):
            chosen = answers.get(fi)
            if not chosen:
                continue
            fctrl = await field["container"].query_selector('[class*="__control"]')
            if not fctrl:
                continue
            await fctrl.click()
            await asyncio.sleep(0.5)
            fopts = await page.query_selector_all('[class*="__option"]:not([class*="disabled"])')
            for fo in fopts:
                fo_text = (await fo.text_content() or "").strip()
                if fo_text.lower() == chosen.lower() or chosen.lower() in fo_text.lower():
                    await fo.click()
                    await asyncio.sleep(0.3)
                    print(f"  ✅ React-Select [{field['label'][:60]}]: {chosen}")
                    break
            else:
                await page.keyboard.press("Escape")
                await asyncio.sleep(0.2)
    except Exception:
        pass


async def _fill_text_inputs_native(page, api_key: str, model_name: str, company: str, salary: str, home_city: str, home_zip: str) -> None:
    """Find empty text inputs (skipping identity/React-Select fields), LLM-fill each one natively."""
    _skip = {
        "first name", "last name", "email", "phone", "linkedin",
        "website", "portfolio", "preferred", "middle", "pronouns", "suffix",
        "cover letter", "location", "city",
    }
    try:
        txt_inputs = await page.query_selector_all(
            'input[type="text"]:not([type="hidden"]):not([disabled]), '
            'input:not([type]):not([disabled])'
        )
        txt_fields = []
        for ti in txt_inputs:
            if not await ti.is_visible():
                continue
            if (await ti.get_attribute("value") or "").strip():
                continue
            is_in_rs = await page.evaluate("""(el) => {
                let node = el;
                for (let i = 0; i < 6; i++) {
                    node = node.parentElement;
                    if (!node) return false;
                    const cls = node.className || '';
                    if (cls.includes('react-select') || cls.includes('select__')) return true;
                }
                return false;
            }""", ti)
            if is_in_rs:
                continue
            lbl = await page.evaluate("""(el) => {
                let node = el;
                for (let i = 0; i < 8; i++) {
                    node = node.parentElement;
                    if (!node) return '';
                    const lbl = node.querySelector('label, [class*="label"]');
                    if (lbl && lbl.textContent.trim()) return lbl.textContent.trim();
                }
                return el.placeholder || el.getAttribute('aria-label') || '';
            }""", ti)
            if not lbl or any(s in lbl.lower() for s in _skip):
                continue
            txt_fields.append({"label": lbl, "element": ti})

        if not txt_fields:
            return

        questions = "\n".join(f'{i+1}. "{f["label"]}"' for i, f in enumerate(txt_fields))
        prompt = (
            f"You are filling a job application at {company}.\n"
            f"Applicant info: city={home_city}, zip={home_zip}, salary={salary}, source=LinkedIn\n\n"
            f"For each text field below, provide the best short answer (1 line max).\n"
            f"If it's a portfolio/design URL and the applicant has none, reply SKIP.\n"
            f"Fields:\n{questions}\n\n"
            f"Reply with ONLY a numbered list:\n1. [answer]\n2. [answer]\netc."
        )
        oai_client = openai.OpenAI(api_key=api_key)
        resp = oai_client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0,
        )
        for tline in resp.choices[0].message.content.strip().splitlines():
            m = re.match(r"^\s*(\d+)\.\s*(.+)$", tline.strip())
            if m:
                ti_idx = int(m.group(1)) - 1
                tv = m.group(2).strip().strip('"').strip("'")
                if 0 <= ti_idx < len(txt_fields) and tv.upper() != "SKIP":
                    try:
                        tf_el = txt_fields[ti_idx]["element"]
                        await tf_el.click()
                        await tf_el.fill("")
                        await tf_el.type(tv, delay=20)
                        print(f"  ✅ Native text [{txt_fields[ti_idx]['label'][:50]}]: {tv}")
                    except Exception:
                        pass
    except Exception:
        pass


async def _fill_city_autocomplete(page, city_name: str) -> None:
    """Fill the geocode-backed Location (City) React-Select with the correct city option."""
    try:
        city_ctrl = await page.evaluate_handle("""() => {
            const ctrls = [...document.querySelectorAll('[class*="__control"]')];
            for (const ctrl of ctrls) {
                let node = ctrl;
                for (let i = 0; i < 12; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const lbl = node.querySelector('label');
                    if (lbl && /location.*(city)|city/i.test(lbl.textContent)) return ctrl;
                }
            }
            return null;
        }""")
        city_el = city_ctrl.as_element() if city_ctrl else None
        if not city_el:
            return
        city_val_el = await city_el.query_selector('[class*="__single-value"]')
        city_cur = (await city_val_el.text_content() or "").strip() if city_val_el else ""
        if city_cur:
            print(f"  ✅ City already set: {city_cur}")
            return
        await city_el.click()
        await asyncio.sleep(0.4)
        city_inp = await city_el.query_selector("input")
        if not city_inp:
            return
        await city_inp.fill("")
        await city_inp.type(city_name[:8], delay=80)
        await asyncio.sleep(2.2)
        city_opts = await page.query_selector_all('[class*="__option"]:not([class*="disabled"])')
        if city_opts:
            city_pick = city_opts[0]
            for co in city_opts:
                if city_name.lower()[:5] in (await co.text_content() or "").lower():
                    city_pick = co
                    break
            city_chosen = (await city_pick.text_content() or "").strip()
            await city_pick.click()
            await asyncio.sleep(0.3)
            print(f"  ✅ City autocomplete: {city_chosen}")
        else:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.2)
            city_clear = await city_el.query_selector('[aria-label*="clear" i], [class*="__clear"]')
            if city_clear:
                await city_clear.click()
            print(f"  ⚠️ City autocomplete: no options, field cleared")
    except Exception as e:
        print(f"  ⚠️ City autocomplete handler: {repr(e)[:80]}")


async def run_executor(job_url: str, user_profile: dict, local_pdf_path: str,
                       company_name: str = "", job_title: str = "",
                       resume_text: str = "", match_score: int = 0,
                       relevance_explanation: str = "", user_id: str = "") -> bool:
    """
    Run the application flow for a single job_url using AsyncStagehand.
    Full 500+ line logic preserved with Stealth + Headful bypasses.
    """

    # --- PHASE 0: THE DETECTIVE ---
    # Strategy A: Query ATS public APIs (Greenhouse/Lever/Ashby) — no proxy, no bot detection
    if any(x in job_url.lower() for x in ["adzuna.com", "ziprecruiter.com", "indeed.com"]) \
            and company_name and job_title:
        print(f"🕵️ Phase-0 ATS lookup for '{job_title}' @ '{company_name}'...")
        try:
            import requests as _req, re as _re

            _stop = {'a', 'an', 'the', 'of', 'for', 'in', 'at', 'and', 'or', 'to', 'on'}
            def _ats_title_match(a, b):
                a, b = a.lower(), b.lower()
                if a in b or b in a:
                    return True
                words_a = {w for w in _re.split(r'[\W_]+', a) if len(w) > 2 and w not in _stop}
                words_b = {w for w in _re.split(r'[\W_]+', b) if len(w) > 2 and w not in _stop}
                return len(words_a & words_b) >= 2

            slug_base = _re.sub(r'[^a-z0-9]+', '-', company_name.lower()).strip('-')
            slugs = list(dict.fromkeys([slug_base, slug_base.replace('-', ''), slug_base.replace('-', '_')]))
            _headers = {"User-Agent": "Mozilla/5.0 ResuMapBot/1.0"}

            ats_url = None
            for slug in slugs:
                if ats_url:
                    break
                # Greenhouse
                try:
                    r = _req.get(f"https://boards.greenhouse.io/v1/boards/{slug}/jobs?content=true", headers=_headers, timeout=8)
                    if r.status_code == 200:
                        for j in r.json().get('jobs', []):
                            if _ats_title_match(job_title, j.get('title', '')) and j.get('absolute_url'):
                                ats_url = j['absolute_url']; break
                except Exception: pass
                # Lever
                if not ats_url:
                    try:
                        r = _req.get(f"https://api.lever.co/v0/postings/{slug}?mode=json", headers=_headers, timeout=8)
                        if r.status_code == 200:
                            for j in r.json():
                                if _ats_title_match(job_title, j.get('text', '')) and j.get('hostedUrl'):
                                    ats_url = j['hostedUrl']; break
                    except Exception: pass
                # Ashby (key is 'jobs', URL is in 'jobUrl')
                if not ats_url:
                    try:
                        r = _req.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", headers=_headers, timeout=8)
                        if r.status_code == 200:
                            for j in r.json().get('jobs', []):
                                if _ats_title_match(job_title, j.get('title', '')) and j.get('jobUrl'):
                                    ats_url = j['jobUrl']; break
                    except Exception: pass

            if ats_url:
                print(f"✅ Phase-0 ATS lookup succeeded: {ats_url}")
                job_url = ats_url
            else:
                print(f"⚠️ Phase-0 ATS lookup found nothing — will try browser-based approach")
        except Exception as e:
            print(f"⚠️ Phase-0 ATS lookup failed: {e}")

    # If still on aggregator after ATS lookup, skip — can't reliably reach employer page
    if any(x in job_url.lower() for x in ["adzuna.com", "ziprecruiter.com", "indeed.com"]):
        print(f"⚠️ Could not resolve employer URL for {job_url[:60]} — skipping")
        return False

    # --- PHASE 1: INITIALIZATION & LOGGING ---
    base_debug = "/home/azureuser/Resumap/tailor-service/debug"
    video_dir = os.path.join(base_debug, "videos")
    for d in [base_debug, video_dir, "resumes"]:
        os.makedirs(d, exist_ok=True)

    executor_key = os.getenv("EXECUTOR_OPENAI_KEY") or os.getenv("AZURE_OPENAI_KEY")
    os.environ.setdefault("MODEL_API_KEY", executor_key or "")
    api_key = os.getenv("MODEL_API_KEY")
    model_name = os.getenv("EXECUTOR_MODEL") or "gpt-4o-mini"

    # Pre-generate gap-fill answers BEFORE sanitizing (needs raw types)
    print("🤖 Pre-generating gap-fill answers from resume + profile...")
    gap_answers = _pre_generate_answers(
        profile=user_profile,
        resume_text=resume_text,
        company_name=company_name,
        job_title=job_title,
        api_key=api_key,
        model=model_name,
    )
    # Merge generated answers into profile (don't overwrite existing real values)
    for k, v in gap_answers.items():
        if v and not user_profile.get(k):
            user_profile[k] = v

    user_profile = sanitize_profile(user_profile)

    chrome_path = os.getenv("CHROME_PATH")
    if not chrome_path:
        for candidate in ("google-chrome", "chromium-browser", "chromium"):
            found = shutil.which(candidate)
            if found:
                chrome_path = found
                break
    os.environ.setdefault("CHROME_PATH", chrome_path or "")

    responses_log = os.path.join(base_debug, "stagehand_responses.log")
    qa_log_dir = os.path.join(base_debug, "form_qa")
    os.makedirs(qa_log_dir, exist_ok=True)

    # Per-job Q&A log — one JSONL file per job run, named by timestamp + company
    _safe_company = "".join(c if c.isalnum() else "_" for c in (company_name or "unknown"))[:40]
    qa_log_path = os.path.join(qa_log_dir, f"{int(time.time())}_{_safe_company}.jsonl")

    def _write_log(name: str, payload):
        def _safe(obj):
            try:
                if obj is None or isinstance(obj, (str, int, float, bool)): return obj
                if isinstance(obj, (list, tuple)): return [_safe(x) for x in obj]
                if isinstance(obj, dict): return {str(k): _safe(v) for k, v in obj.items()}
                if hasattr(obj, "data"): return _safe(getattr(obj, "data"))
                return str(obj)
            except: return "<unserializable>"
        try:
            entry = {"ts": int(time.time()), "name": name, "payload": _safe(payload)}
            with open(responses_log, "a") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except: pass

    # --- Start Xvfb virtual display (headful Chrome, reduces bot fingerprinting) ---
    _display = f":{os.getpid() % 200 + 100}"  # per-PID display, supports parallel runs
    _xvfb_proc = None
    try:
        _xvfb_proc = subprocess.Popen(
            ["Xvfb", _display, "-screen", "0", "1920x1080x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = _display
        await asyncio.sleep(0.5)
        print(f"🖥️ Xvfb started on display {_display}")
    except Exception as e:
        print(f"⚠️ Xvfb start failed ({e}) — falling back to headless")
        _xvfb_proc = None

    _headless = _xvfb_proc is None  # headless only if Xvfb failed

    # --- PHASE 2: STAGEHAND SESSION (v3.6+ API) ---
    async with AsyncStagehand(
        server="local",
        model_api_key=api_key,
        local_openai_api_key=api_key,
        local_headless=_headless,
        local_chrome_path=chrome_path or None,
        local_ready_timeout_s=30.0,
    ) as client:
        session_resp = await client.sessions.start(
            model_name=model_name,
            browser={
                "type": "local",
                "launch_options": {
                    "headless": _headless,
                    "args": [
                        "--no-sandbox", "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1920,1080",
                    ],
                },
            },
        )
        sid = session_resp.data.session_id
        print(f"✅ Stagehand session started: {sid}")

        async def act(instruction: str, variables: dict | None = None):
            params = {"id": sid, "input": instruction}
            if variables:
                params["options"] = {"variables": {k: str(v) for k, v in variables.items()}}
            _write_log("act_request", {"input": instruction})
            for attempt in range(3):
                try:
                    res = await client.sessions.act(**params)
                    _write_log("act_response", {"success": res.success, "data": str(res.data)[:300]})
                    return res
                except Exception as e:
                    _write_log("act_error", {"attempt": attempt, "error": repr(e)})
                    if attempt == 2: raise
                    await asyncio.sleep(2 ** attempt)

        async def observe(instruction: str):
            _write_log("observe_request", {"instruction": instruction})
            res = await client.sessions.observe(id=sid, instruction=instruction)
            _write_log("observe_response", {"data": str(res.data)[:300]})
            raw = str(res.data).lower()
            return any(w in raw for w in (
                "true", "yes", "success", "submitted", "thank",
                "received", "confirmation", "complete", "applied",
                "we'll be in touch", "application sent"
            ))

        async def navigate(url: str):
            await client.sessions.navigate(id=sid, url=url)

        async def log_form_state(step: str):
            """Extract all visible form fields + current values and write to per-job Q&A log."""
            try:
                res = await asyncio.wait_for(client.sessions.extract(
                    id=sid,
                    instruction=(
                        "List every visible form field on the page. For each field return: "
                        "the label or question text, the field type (text/dropdown/checkbox/radio/textarea), "
                        "and the current value or selected option. "
                        "If a field is empty or unanswered, set value to null. "
                        "Return as a JSON array of objects with keys: label, type, value."
                    ),
                ), timeout=15)
                raw = str(res.data) if res and res.data else ""
                # Try to parse the JSON array Stagehand returns inside the extraction string
                fields = []
                try:
                    m = re.search(r'\[.*\]', raw, re.DOTALL)
                    if m:
                        fields = json.loads(m.group())
                except Exception:
                    fields = [{"raw": raw[:2000]}]

                entry = {
                    "ts": int(time.time()),
                    "step": step,
                    "job_url": job_url,
                    "company": company_name,
                    "job_title": job_title,
                    "fields": fields,
                }
                with open(qa_log_path, "a") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                print(f"📋 Q&A logged ({len(fields)} fields) → {step}")
            except Exception as e:
                print(f"⚠️ log_form_state failed at '{step}': {e}")

        # Connect Playwright to Stagehand's browser for native file upload
        cdp_url = session_resp.data.cdp_url
        _pw_browser = None
        _pw_context = None
        if cdp_url:
            try:
                from playwright.async_api import async_playwright as _apw
                _pw = await _apw().__aenter__()
                _pw_browser = await _pw.chromium.connect_over_cdp(cdp_url)
                _pw_context = _pw_browser.contexts[0] if _pw_browser.contexts else None
            except Exception as e:
                print(f"⚠️ CDP connect failed: {e}")

        def _pw_page():
            """Return the active Playwright page (last page in context)."""
            if not _pw_context:
                return None
            pages = _pw_context.pages
            return pages[-1] if pages else None

        async def _native_fill(selectors: list, value: str, label: str = "") -> bool:
            """
            Fill a form field natively via Playwright CDP using a list of CSS selectors.
            Tries each selector in order; returns True on first success.
            Uses el.type() (character-by-character) to properly trigger React synthetic events.
            """
            page = _pw_page()
            if not page or not value:
                return False
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.click()
                        await el.fill("")       # clear existing value
                        await el.type(value, delay=30)   # type char-by-char to trigger React events
                        print(f"  ✅ native fill [{label or sel}]: {value[:50]}")
                        return True
                except Exception:
                    continue
            return False

        async def _native_select(selectors: list, value: str, label: str = "") -> bool:
            """Select an option from a <select> dropdown natively."""
            page = _pw_page()
            if not page or not value:
                return False
            for sel in selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await page.select_option(sel, label=value)
                        print(f"  ✅ native select [{label or sel}]: {value[:50]}")
                        return True
                except Exception:
                    try:
                        # fallback: partial label match
                        opts = await page.query_selector_all(f"{sel} option")
                        for opt in opts:
                            txt = (await opt.text_content() or "").strip()
                            if value.lower() in txt.lower():
                                val = await opt.get_attribute("value")
                                await page.select_option(sel, value=val)
                                print(f"  ✅ native select partial [{label}]: {txt[:50]}")
                                return True
                    except Exception:
                        continue
            return False

        async def _native_upload(pdf_path: str) -> bool:
            """Try native Playwright set_input_files; returns True on success."""
            page = _pw_page()
            if not page:
                return False
            try:
                for sel in ["input[type=file]", "input[name*='resume']",
                            "input[id*='resume']", "input[name*='cv']"]:
                    el = await page.query_selector(sel)
                    if el:
                        await el.set_input_files(pdf_path)
                        print("✅ Native upload success.")
                        return True
            except Exception as e:
                print(f"⚠️ Native upload error: {e}")
            return False

        # --- Start Playwright tracing for replay/debugging ---
        trace_path = os.path.join(base_debug, f"trace_{int(time.time())}_{_safe_company}.zip")
        if _pw_context:
            try:
                await _pw_context.tracing.start(screenshots=True, snapshots=True)
                print(f"🎥 Tracing started → {trace_path}")
            except Exception as e:
                print(f"⚠️ Tracing start failed: {e}")

        # --- PHASE 3: EXECUTION FLOW ---
        print(f"🔗 Navigating to: {job_url}")
        await navigate(job_url)
        await asyncio.sleep(6)

        # Open the application form — skip cookie banner step (it was clicking Apply)
        print("🖱️ Opening application form...")
        await act(
            "Find and click the primary job application button on this page. "
            "It is typically labelled 'Apply for this job', 'Apply now', or 'Apply'. "
            "It is a prominent button near the job title or at the top-right of the posting. "
            "Do NOT click: 'Sign in', 'Log in', 'Create account', 'Apply with LinkedIn', "
            "'Apply with Indeed', or any OAuth/social button. Click only the direct Apply button."
        )
        await asyncio.sleep(8)  # Greenhouse/Lever iframes need time to fully load

        # Store gate keywords for reuse in the post-submit verification flow
        _GATE_KEYWORDS = [
            "enter the code", "verification code", "we sent you a code",
            "check your email for a code", "enter your code",
            "security code", "please verify your email",
        ]
        _app_pw = (
            user_profile.get("email_app_password") or
            user_profile.get("emailAppPassword") or
            os.getenv("GMAIL_APP_PASSWORD") or ""
        )

        # NOTE: Greenhouse sends the verification code AFTER submission, not after Apply click.
        # Detection and handling is therefore done post-submit (see submission section below).
        # The sign-in bypass step is intentionally removed — it was misidentifying the Apply
        # button as a "Continue as guest" target and re-clicking it, causing a page reload.

        # --- Fill form fields: native Playwright for predictable fields,
        #     Stagehand only for dynamic/intelligent content ---
        print("🛠️ Filling form fields (native CDP)...")

        p = user_profile  # shorthand
        first = p.get("legal_first_name") or p.get("legalFirstName") or p.get("first_name", "")
        last  = p.get("legal_last_name")  or p.get("legalLastName")  or p.get("last_name", "")
        full_name = p.get("full_name") or (first + " " + last).strip()
        phone = p.get("phone_with_code") or p.get("phoneWithCode") or (
            (p.get("phone_country_code") or p.get("phoneCountryCode") or "") +
            (p.get("phone") or "")
        )
        email    = p.get("email", "")
        linkedin = p.get("linkedin_url") or p.get("linkedinUrl") or ""
        website  = p.get("portfolio_url") or p.get("portfolioUrl") or p.get("github_url") or p.get("githubUrl") or ""
        school   = p.get("school_name") or p.get("schoolName") or ""
        degree   = p.get("highest_education") or p.get("educationLevel") or ""

        # --- Native fill: identity fields ---
        # Greenhouse, Lever, Ashby all use <input> with predictable id/name patterns.
        for _label, _val, _sels in [
            ("First Name", first, ["input#first_name", "input[name='first_name']", "input[autocomplete='given-name']", "input[placeholder*='First']", "input[aria-label*='First Name']"]),
            ("Last Name",  last,  ["input#last_name",  "input[name='last_name']",  "input[autocomplete='family-name']", "input[placeholder*='Last']",  "input[aria-label*='Last Name']"]),
            ("Full Name",  full_name, ["input[name='name']", "input#name", "input[placeholder*='Full name']", "input[placeholder*='Your name']", "input[aria-label*='Full Name']"]),
            ("Email",      email,    ["input#email",  "input[name='email']",  "input[type='email']",  "input[autocomplete='email']",  "input[placeholder*='Email']"]),
            ("Phone",      phone,    ["input#phone",  "input[name='phone']",  "input[type='tel']",    "input[autocomplete='tel']",    "input[placeholder*='Phone']"]),
            ("LinkedIn",   linkedin, ["input[name*='linkedin']", "input[id*='linkedin']", "input[placeholder*='LinkedIn']", "input[aria-label*='LinkedIn']", "input[name*='question'][name*='text']"]),
            ("Website",    website,  ["input[name*='website']",  "input[id*='website']",  "input[name*='portfolio']", "input[placeholder*='Website']", "input[placeholder*='Portfolio']"]),
        ]:
            await _native_fill(_sels, _val, _label)

        # Country — works across ATS platforms:
        # 1. Text input (modern Greenhouse uses input#country with autocomplete)
        # 2. Standard <select> (Lever, older ATS)
        # 3. Stagehand visual AI for any custom component (React-Select, custom dropdowns)
        country_filled = await _native_fill([
            "input#country", "input[name='country']", "input[id*='country']",
            "input[name*='country']", "input[placeholder*='Country']",
        ], "United States", "Country")

        if not country_filled:
            country_filled = await _native_select([
                "select#country", "select[name='country']", "select[name*='country']",
                "select[id*='country']",
            ], "United States", "Country")

        if not country_filled:
            try:
                await act(
                    "Find the field labeled 'Country' on this application form. "
                    "Click on it and select or type 'United States'. "
                    "Do NOT click Submit or any button."
                )
                print("  🤖 Country: Stagehand act attempted")
            except Exception:
                pass

        await log_form_state("basic_fields")

        # --- Resume upload ---
        if os.path.exists(local_pdf_path):
            print(f"📤 Uploading resume: {local_pdf_path}")
            uploaded = await _native_upload(local_pdf_path)
            if not uploaded:
                await act(
                    f"Find the file upload button labelled 'Resume/CV', 'Upload resume', or 'Attach resume' "
                    f"and upload the file at: {local_pdf_path}"
                )
            await asyncio.sleep(2)

        # --- Education ---
        if school:
            filled = await _native_fill([
                "input[name*='school']", "input[id*='school']",
                "input[placeholder*='School']", "input[aria-label*='School']",
                "input[name*='university']", "input[id*='university']",
            ], school, "School")
            # Greenhouse school is a typeahead — select the first autocomplete option
            if filled:
                page = _pw_page()
                if page:
                    await asyncio.sleep(1)
                    try:
                        # Dismiss typeahead by pressing Enter or clicking first option
                        opt = await page.query_selector("li[role='option'], .autocomplete-item, [class*='suggestion']")
                        if opt:
                            await opt.click()
                        else:
                            await page.keyboard.press("Enter")
                    except Exception:
                        pass

        if degree:
            await _native_select([
                "select#education", "select[name*='education']", "select[id*='education']",
                "select[name*='degree']", "select[id*='degree']",
            ], degree, "Degree")

        # --- Work auth & sponsorship via native select ---
        await _native_select([
            "select[name*='authorized']", "select[id*='authorized']",
            "select[name*='work_auth']", "select[id*='work_auth']",
            "select[name*='eligible']",
        ], "Yes", "Work Authorization")

        await _native_select([
            "select[name*='sponsor']", "select[id*='sponsor']",
            "select[name*='visa']", "select[id*='visa']",
        ], "No", "Sponsorship")

        await asyncio.sleep(1)

        # --- EEO voluntary disclosures via native select ---
        eeo_selectors = {
            "Gender": ([
                "select[name*='gender']", "select[id*='gender']",
            ], p.get("gender") or "Decline to Self-Identify"),
            "Race/Ethnicity": ([
                "select[name*='race']", "select[id*='race']",
                "select[name*='ethnicity']", "select[id*='ethnicity']",
            ], p.get("race") or p.get("raceEthnicity") or "Decline to Self-Identify"),
            "Hispanic/Latino": ([
                "select[name*='hispanic']", "select[id*='hispanic']",
                "select[name*='latino']",
            ], "No"),
            "Veteran Status": ([
                "select[name*='veteran']", "select[id*='veteran']",
            ], p.get("veteran_status") or p.get("veteranStatus") or "I am not a protected veteran"),
            "Disability": ([
                "select[name*='disab']", "select[id*='disab']",
            ], p.get("disability_status") or p.get("disabilityStatus") or "No, I do not have a disability"),
        }
        for eeo_label, (sels, eeo_val) in eeo_selectors.items():
            await _native_select(sels, eeo_val, eeo_label)

        await log_form_state("eeo_and_cover_letter")

        # Cover letter — native fill first (fast), Stagehand only if textarea exists but native fails
        cover_letter_text = (
            p.get("generated_cover_letter") or
            f"I am excited to apply for the {job_title or 'role'} at {company_name or 'your company'}. "
            f"With my background as {p.get('headline', 'a product leader')}, "
            f"I am confident I can make a meaningful impact from day one."
        )
        cover_filled = await _native_fill([
            "textarea[name*='cover']", "textarea[id*='cover']",
            "textarea[name*='letter']", "textarea[id*='letter']",
            "textarea[placeholder*='over']",  # "cover letter"
        ], cover_letter_text, "Cover Letter")
        if not cover_filled:
            # Only call Stagehand if a textarea actually exists on the page (avoids 3×100s timeout)
            _cl_page = _pw_page()
            _has_textarea = False
            if _cl_page:
                try:
                    _has_textarea = bool(await _cl_page.query_selector("textarea"))
                except Exception:
                    pass
            if _has_textarea:
                try:
                    await act(
                        f"Find the Cover Letter textarea and type this text: "
                        f"{cover_letter_text[:400]}. Do NOT click Submit."
                    )
                except Exception:
                    pass

        # --- Screening questions: salary, domain experience, company-fit, etc. ---
        print("🤖 Answering screening questions...")

        # Build rich context from all available profile data + pre-generated answers
        salary_answer = (
            p.get("minimumSalary") or p.get("minimum_salary") or
            p.get("generated_salary") or "160000"
        )
        motivation_answer = p.get("generated_motivation") or (
            f"I am deeply interested in joining {company_name or 'your company'} as a {job_title or 'this role'}. "
            f"My background in {p.get('headline', 'product management')} aligns well with this opportunity "
            f"and I am excited about the impact I can drive."
        )
        screening_ctx = {
            "company_name": company_name or "the company",
            "job_title": job_title or "this role",
            "salary_answer": salary_answer,
            "motivation_answer": motivation_answer,
            "full_name": full_name,
            **{k: v for k, v in p.items()},
        }

        # --- Targeted screening acts (avoids the "stuck on Preferred First Name" loop) ---
        # Handle specific question categories one by one rather than a generic loop

        # 1. Any yes/no or select dropdowns about work auth / sponsorship / relocation
        # NOTE: these instructions must be very specific to avoid picking up EEO/demographic dropdowns
        for q_instruction in [
            "Find a dropdown specifically asking whether you are LEGALLY AUTHORIZED TO WORK in the United States "
            "(e.g. 'Are you authorized to work in the US?', 'Are you eligible to work in the US?'). "
            "This is about legal work authorization, NOT demographics or race/ethnicity. Select 'Yes'. "
            "Skip if not found or already answered. Do NOT click Submit.",
            "Find a dropdown specifically asking about VISA SPONSORSHIP — whether you need the company to sponsor "
            "a work visa to employ you (e.g. 'Do you require sponsorship?'). Select 'No'. "
            "Skip if not found or already answered. Do NOT click Submit.",
            "Find a dropdown asking if you are OPEN TO RELOCATION or willing to relocate. Select 'Yes'. "
            "Skip if not found or already answered. Do NOT click Submit.",
        ]:
            try:
                await act(q_instruction)
            except Exception:
                pass

        # 2. Remaining unanswered SELECT dropdowns (custom questions, not Preferred First Name)
        _co = company_name or "the company"
        _sal = salary_answer

        # Universal React-Select filler: LLM reasoning + native Playwright interaction.
        # Architecture: decouple "what answer to pick" (LLM) from "how to click" (native Playwright).
        # This handles ANY custom dropdown on ANY form without per-field keyword enumeration.
        # Stagehand's act() finds these correctly but the option click doesn't always commit to
        # React's state — native Playwright with explicit timing is reliable.
        # NOTE: EEO fields (degree, gender, race, veteran, disability) are intentionally handled
        # here rather than via Stagehand pre-fill acts. Stagehand acts return false success for
        # React-Select and leave dropdowns open, causing the native filler's toggle click to
        # close them instead of open them. The LLM prompt below includes EEO defaults.

        # Press Escape to ensure no dropdown is open before starting the native filler
        _rs_pre_escape_page = _pw_page()
        if _rs_pre_escape_page:
            try:
                await _rs_pre_escape_page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        _rs_page = _pw_page()
        if _rs_page and api_key:
            await _fill_react_selects_native(_rs_page, api_key, model_name, p, _co, _sal)

        # Stagehand fallback loop for any non-React-Select dropdowns not yet handled
        for _round in range(3):
            try:
                res = await act(
                    f"Look for any REQUIRED dropdown (marked with *) still showing 'Select...' "
                    f"that is NOT a React-Select custom component (i.e. a standard HTML select or "
                    f"other custom widget). SKIP: Country, Phone, First Name, Last Name, Email, "
                    f"LinkedIn, Degree, Gender, Race, Ethnicity, Veteran Status, Disability Status.\n"
                    f"If you find one, pick the best answer:\n"
                    f"- Work authorization → Yes\n"
                    f"- Sponsorship → No\n"
                    f"- Relocation / onsite / hybrid → Yes\n"
                    f"- Currently at {_co} → No\n"
                    f"- Start date → Immediately\n"
                    f"- Any other yes/no → Yes\n"
                    f"- Any other → most positive option\n"
                    f"If nothing left unfilled, respond 'DONE'. Do NOT click Submit."
                )
                res_text = str(res.data).lower() if res else ""
                if "done" in res_text:
                    print(f"  ✅ Dropdown screening done after {_round+1} fallback rounds")
                    break
            except Exception as e:
                _write_log("screening_loop_error", {"round": _round, "error": repr(e)})
                break

        # 3. Any open-ended TEXT questions (not standard identity fields)
        _motivation = motivation_answer
        _headline = p.get("headline") or p.get("generated_summary") or "product management"
        _home_city = p.get("city") or "Mountain View"
        _home_zip = p.get("zipCode") or p.get("zip") or p.get("postalCode") or "94043"

        # Native pass for required text inputs (skips identity/React-Select fields, LLM-fills the rest)
        _txt_page = _pw_page()
        if _txt_page and api_key:
            await _fill_text_inputs_native(_txt_page, api_key, model_name, _co, _sal, _home_city, _home_zip)

        # Stagehand fallback for any remaining text fields not caught natively
        try:
            await act(
                f"Look for any REQUIRED text input or textarea (marked with *) that is still empty. "
                f"A required field has an asterisk (*) directly next to its label text. "
                f"NEVER fill: Preferred First Name, Preferred Name, Middle Name, Pronouns, Suffix. "
                f"Ignore standard identity fields: First Name, Last Name, Email, Phone, LinkedIn, Website. "
                f"Fill each genuinely required empty field using the right answer for its type:\n"
                f"- Zip Code / Postal Code / ZIP of residence → {_home_zip}\n"
                f"- How did you hear about this job / referral source / where did you find this role → LinkedIn\n"
                f"- 'Why this company' / 'Why are you interested' / motivation → {_motivation}\n"
                f"- Salary / compensation expectation → {_sal}\n"
                f"- Portfolio / design portfolio URL → leave blank (skip this field)\n"
                f"- Any other short text field (single line) → a brief, direct answer\n"
                f"- Any other long text / textarea → 2-3 confident sentences about: {_headline}\n"
                f"If no required empty text fields exist, do nothing. Do NOT click Submit."
            )
        except Exception:
            pass

        await log_form_state("screening_questions")

        # Handle Location (City) async geocode autocomplete (type + wait for API + click option)
        _city_page = _pw_page()
        if _city_page and _home_city:
            await _fill_city_autocomplete(_city_page, _home_city)

        # Submit — close any open React-Select dropdowns first, then click Submit natively
        print("🚀 Submitting...")
        _submit_time = time.time()

        # Press Escape to close any open dropdown menus before submitting
        _pre_submit_page = _pw_page()
        if _pre_submit_page:
            try:
                await _pre_submit_page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
            except Exception:
                pass

        # Try native Playwright click on the submit button (more reliable than Stagehand for final submit)
        _native_submit_ok = False
        _submit_page_native = _pw_page()
        if _submit_page_native:
            for _sbtn_sel in [
                "button:has-text('Submit application')",
                "button:has-text('Submit Application')",
                "input[type='submit']",
                "button[type='submit']",
                "button:has-text('Submit')",
                "button:has-text('Apply')",
            ]:
                try:
                    _sbtn = await _submit_page_native.query_selector(_sbtn_sel)
                    if _sbtn and await _sbtn.is_visible():
                        await _sbtn.scroll_into_view_if_needed()
                        await asyncio.sleep(0.5)
                        await _sbtn.click()
                        _native_submit_ok = True
                        print(f"  🖱️ Native submit click via: {_sbtn_sel}")
                        break
                except Exception:
                    pass

        if not _native_submit_ok:
            # Fallback to Stagehand
            await act(
                "Click the final Submit or Apply button to submit the application. "
                "This button is typically labelled 'Submit Application', 'Submit', or 'Apply'. "
                "Do NOT click any sign-in, login, or social login button. "
                "If a sign-in prompt appears before submitting, look for 'Continue as guest' or 'Skip' first."
            )
        await asyncio.sleep(8)

        # Verify — check page content first (fast), then Stagehand observe
        success = False
        _post_page = _pw_page()
        if _post_page:
            try:
                _post_content = (await _post_page.content()).lower()
                _post_url = _post_page.url.lower()
                _pg_clean = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', ' ', _post_content, flags=re.DOTALL)
                _pg_text = re.sub(r'<[^>]+>', ' ', _pg_clean)
                _pg_text = re.sub(r'\s+', ' ', _pg_text).strip()
                print(f"  📄 Post-submit URL: {_post_url[:100]}")
                print(f"  📄 Post-submit text: {_pg_text[:600]}")

                # --- Verification code gate: Greenhouse sends code on submit, not on Apply ---
                if any(kw in _post_content for kw in _GATE_KEYWORDS):
                    print("📧 Email verification code required after submit — checking inbox...")
                    if _app_pw:
                        _code = await _read_verification_code_from_email(
                            email, _app_pw, timeout_secs=90,
                            min_timestamp=_submit_time,
                        )
                        if _code:
                            try:
                                # Try native Playwright fill: find code input inside security fieldset
                                # NOT input[type='text'][maxlength] which matches First Name on Greenhouse
                                _native_code_filled = False
                                _vcode_page = _pw_page()
                                if _vcode_page:
                                    try:
                                        _code_el = await _vcode_page.evaluate_handle("""() => {
                                            // Search fieldsets for the one containing security/verification text
                                            for (const fs of [...document.querySelectorAll('fieldset')].reverse()) {
                                                if (/security code|verification code|enter.*code|confirm.*code/i.test(fs.textContent)) {
                                                    return fs.querySelector('input[type="text"], input:not([type="hidden"])') || null;
                                                }
                                            }
                                            // Fallback: explicit name/id/placeholder attributes
                                            return (
                                                document.querySelector('input[name*="code" i]') ||
                                                document.querySelector('input[id*="code" i]') ||
                                                document.querySelector('input[placeholder*="code" i]')
                                            ) || null;
                                        }""")
                                        # evaluate_handle returns null handle when JS returns null
                                        _is_null = await _vcode_page.evaluate("el => el === null", _code_el)
                                        if not _is_null:
                                            await _code_el.fill("")
                                            await _code_el.type(_code, delay=50)
                                            print(f"  ✏️ Native code fill via fieldset search")
                                            _native_code_filled = True
                                    except Exception as _ce:
                                        print(f"  ⚠️ Native code field search failed: {_ce}")
                                if not _native_code_filled:
                                    await act(
                                        f"Find the security code or verification code input field on the page. "
                                        f"Clear it and type exactly: {_code}. "
                                        f"Then click the 'Submit' or 'Resubmit' button."
                                    )
                                else:
                                    # Native-filled — now click submit/resubmit
                                    _clicked_submit = False
                                    if _vcode_page:
                                        for btn_sel in [
                                            "button[type='submit']", "input[type='submit']",
                                            "button:has-text('Submit')", "button:has-text('Resubmit')",
                                        ]:
                                            try:
                                                btn = await _vcode_page.query_selector(btn_sel)
                                                if btn and await btn.is_visible():
                                                    await btn.click()
                                                    print(f"  🖱️ Clicked submit via {btn_sel}")
                                                    _clicked_submit = True
                                                    break
                                            except Exception:
                                                continue
                                    if not _clicked_submit:
                                        await act("Click the Submit or Resubmit button to submit the application.")
                                await asyncio.sleep(8)
                                # Re-read page after resubmit
                                _cur_page = _pw_page() or _post_page
                                _post_content = (await _cur_page.content()).lower()
                                _post_url = _cur_page.url.lower()
                                # Print stripped text to understand page state
                                _page_text = re.sub(r'<[^>]+>', ' ', _post_content)[:400]
                                print(f"  ✅ Code {_code} entered — URL: {_post_url[:80]}")
                                print(f"  Page text: {_page_text.strip()[:300]}")
                            except Exception as e:
                                print(f"  ⚠️ Code entry failed: {e}")
                        else:
                            print("⚠️ No code found in inbox within 90s")
                    else:
                        print("⚠️ No email app password in profile — cannot auto-read code")

                # Check success keywords
                success = any(kw in _post_content for kw in (
                    "application was submitted", "thank you for applying",
                    "your application has been received", "we've received your application",
                    "successfully submitted", "application received",
                    "we will be in touch", "we'll review your application",
                    "application submitted", "successfully applied",
                )) or any(kw in _post_url for kw in ("confirmation", "thank", "success", "submitted"))
                if success:
                    print("  ✅ Confirmed via page content/URL check")
            except Exception:
                pass

        if not success:
            success = await observe("Is there a success or thank you confirmation message on the page?")

        print(f"{'✅ Submitted!' if success else '⚠️ Could not confirm submission'}")

        # Write final outcome to Q&A log so the record is complete
        try:
            outcome = {
                "ts": int(time.time()),
                "step": "submission_result",
                "job_url": job_url,
                "company": company_name,
                "job_title": job_title,
                "submitted": success,
            }
            with open(qa_log_path, "a") as fh:
                fh.write(json.dumps(outcome, ensure_ascii=False) + "\n")
        except Exception:
            pass

        # Append to centralized applications log for deduplication by scout
        _apps_log_path = os.path.join(os.path.dirname(__file__), "applications_log.jsonl")
        try:
            import json as _json_log
            with open(_apps_log_path, "a") as _alf:
                _alf.write(_json_log.dumps({
                    "ts": int(time.time()),
                    "user_email": p.get("email") or "",
                    "job_url": job_url,
                    "company": company_name,
                    "job_title": job_title,
                    "submitted": success,
                }) + "\n")
        except Exception:
            pass

        # POST application result to Replit dashboard (only on successful submissions)
        _replit_url = os.getenv("REPLIT_URL", "")
        _webhook_secret = os.getenv("WEBHOOK_SECRET", "")
        if success and _replit_url and _webhook_secret:
            try:
                # Read all answered form fields from the Q&A log
                _qa_fields: dict = {}  # label -> last non-null value
                try:
                    with open(qa_log_path) as _qf:
                        for _ql in _qf:
                            try:
                                _qe = json.loads(_ql.strip())
                                if _qe.get("step") == "submission_result":
                                    continue
                                for _field in (_qe.get("fields") or []):
                                    _lbl = (_field.get("label") or "").strip()
                                    _val = _field.get("value")
                                    if _lbl and _val not in (None, "", [], {}):
                                        _qa_fields[_lbl] = str(_val)
                            except Exception:
                                pass
                except Exception:
                    pass

                _wh_payload = {
                    "userId": user_id,
                    "jobTitle": job_title,
                    "company": company_name,
                    "status": "Applied" if success else "Failed",
                    "jobUrl": job_url,
                    "matchScore": match_score,
                    "relevanceExplanation": relevance_explanation,
                    "questionsAnswers": [{"question": k, "answer": v} for k, v in _qa_fields.items()],
                }
                import requests as _req_wh
                _req_wh.post(
                    f"{_replit_url}/api/webhooks/application",
                    json=_wh_payload,
                    headers={"X-Webhook-Secret": _webhook_secret, "Content-Type": "application/json"},
                    timeout=10,
                )
                print(f"  📡 Webhook posted to Replit ({_wh_payload['status']})")
            except Exception as _whe:
                print(f"  ⚠️ Webhook post failed: {repr(_whe)[:80]}")

        # Save Playwright trace for visual replay
        if _pw_context:
            try:
                await _pw_context.tracing.stop(path=trace_path)
                print(f"🎥 Trace saved → {trace_path}")
            except Exception as e:
                print(f"⚠️ Tracing save failed: {e}")

        await client.sessions.end(id=sid)

    # Cleanup Xvfb virtual display
    if _xvfb_proc is not None:
        try:
            _xvfb_proc.terminate()
        except Exception:
            pass

    return success