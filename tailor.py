import os
import re
import json
import asyncio
from openai import AsyncAzureOpenAI  # Changed this
from dotenv import load_dotenv     # Added this

# Load the .env file so os.environ can see your Azure keys
load_dotenv()

_client: AsyncAzureOpenAI | None = None

def get_client() -> AsyncAzureOpenAI:
    global _client
    if _client is None:
  # We now pull Azure-specific variables from your .env
        _client = AsyncAzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ["AZURE_OPENAI_VERSION"]
        )
    return _client


def _clean_na(val: str | None) -> str:
    if not val:
        return ""
    return "" if re.match(r"^n\/?a$", val.strip(), re.IGNORECASE) else val


_TAG_RE = re.compile(r"\s*\[[^\]]*B\d+[^\]]*\]\s*\.?\s*")
_SOFT_ALIGNED_RE = re.compile(r"^\s*softAligned\s*:\s*\[.*?\]\s*$", re.IGNORECASE)


def _strip_tags(s: str) -> str:
    if not s:
        return s
    s = _TAG_RE.sub("", s)
    s = _SOFT_ALIGNED_RE.sub("", s)
    return s.strip()


def _extract_bullets(extracted_text: str) -> list[str]:
    bullet_lines: list[str] = []
    text_lines = extracted_text.split("\n")
    i = 0
    while i < len(text_lines):
        line = text_lines[i]
        if re.match(r"^\s*[●•]\s", line):
            full_bullet = re.sub(r"^\s*[●•]\s*", "", line).strip()
            j = i + 1
            while j < len(text_lines):
                nxt = text_lines[j].strip()
                if not nxt:
                    break
                if re.match(r"^[●•]\s", nxt):
                    break
                if re.match(r"^[A-Z][A-Z\s&]+$", nxt):
                    break
                if re.match(r"^[A-Z][a-z].*,\s+[A-Z]", nxt):
                    break
                if re.match(r"^\w.*\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s*'?\d", nxt):
                    break
                if (len(nxt) < 60 and not nxt.endswith(".")
                        and re.match(r"^[A-Z]", nxt)
                        and not re.match(r"^\d", nxt)
                        and "●" not in nxt):
                    break
                full_bullet += " " + nxt
                i = j
                j += 1
            bullet_lines.append(full_bullet)
        i += 1
    return bullet_lines


def _build_full_optimized_text(structured_resume: dict) -> str:
    parts: list[str] = []
    if structured_resume.get("name"):
        parts.append(structured_resume["name"])
    if structured_resume.get("contactInfo"):
        parts.append(re.sub(r"\s*\|\s*", "  |  ", structured_resume["contactInfo"]))
    if structured_resume.get("summary"):
        parts.append("")
        parts.append("PROFESSIONAL SUMMARY")
        parts.append(structured_resume["summary"])
    for section in structured_resume.get("sections", []):
        parts.append("")
        parts.append((section.get("title") or "").upper())
        for item in section.get("items", []):
            if section.get("type") == "skills":
                if item.get("label") and item.get("value"):
                    parts.append(f"{item['label']}: {item['value']}")
            else:
                header_parts = []
                if item.get("title"):
                    header_parts.append(item["title"])
                if item.get("subtitle"):
                    header_parts.append(item["subtitle"])
                if item.get("location"):
                    header_parts.append(item["location"])
                if item.get("dateRange"):
                    header_parts.append(item["dateRange"])
                if header_parts and not item.get("isSubsection"):
                    parts.append("  |  ".join(header_parts))
                elif item.get("isSubsection") and item.get("title"):
                    parts.append(item["title"])
                for bullet in item.get("bullets", []):
                    parts.append(f"• {bullet}")
    return "\n".join(parts)


def _build_call2_system_prompt(
    bullet_lines: list[str],
    experience_bullet_count: int,
    skills_bullet_count: int,
    needs_compression: bool,
    retry_attempt: int,
) -> str:
    retry_warning = ""
    if retry_attempt > 0:
        retry_warning = (
            f"\n\nCRITICAL WARNING: A previous attempt returned far too few bullets. "
            f"You MUST include ALL {experience_bullet_count} experience bullets and ALL sections "
            f"from the original resume. Do NOT truncate, summarize, or skip any content. "
            f"Output the COMPLETE resume.\n"
        )

    if not needs_compression:
        two_page_rule = (
            f"THIS RESUME FITS ON 2 PAGES. Do NOT compress, merge, or remove ANY experience bullets. "
            f"Your output MUST contain EXACTLY {experience_bullet_count} experience bullets across all "
            f"experience/generic sections. Skills entries [B{len(bullet_lines) - skills_bullet_count + 1}]-"
            f"[B{len(bullet_lines)}] should be formatted as label/value pairs, not bullets. "
            f"If your output has fewer than {experience_bullet_count} experience bullets, you have made an error."
        )
    else:
        two_page_rule = (
            "THIS RESUME LIKELY EXCEEDS 2 PAGES. Apply this graduated compression IN ORDER:\n"
            "   Step A) For roles OLDER than 10 years OR not aligned with the job description, compress to a "
            "MAXIMUM of 2 bullets per role. Pick the 2 strongest/most transferable bullets and soft-align them. "
            "List removed bullets in \"removedBullets\" array with bullet number and reason.\n"
            "   Step B) If it STILL exceeds 2 pages after Step A, shorten remaining bullet descriptions — "
            "make them more concise without losing key metrics/achievements.\n"
            "   Step C) Only as a LAST RESORT, remove entire job entries from the oldest/least relevant roles. "
            "NEVER remove a job entry if Steps A-B are sufficient.\n"
            "   IMPORTANT: Always preserve the career timeline. Keep every job title, company, and date range "
            "visible even if you reduce bullets. Prioritize the most recent 10 years and JD-aligned roles with "
            "full bullet detail."
        )

    return f"""You are an expert resume optimizer. Your task: take the original resume and a job description, then output a structured JSON resume with optimized bullet points.{retry_warning}

Return valid JSON with this exact shape:
{{
  "name": "Full Name",
  "contactInfo": "email | phone | location | LinkedIn URL — separated by pipes",
  "summary": "Optimized professional summary paragraph tailored to the job",
  "sections": [
    {{
      "title": "Section Name",
      "type": "experience | education | skills | generic",
      "items": [
        {{
          "title": "Role Title",
          "subtitle": "Company Name",
          "dateRange": "Start - End",
          "location": "City, Country",
          "bullets": ["Achievement-focused bullet text here", "Another accomplishment bullet here"]
        }}
      ]
    }}
  ]
}}

For skills-type sections, use: {{ "label": "Category", "value": "comma-separated skills" }}

SUB-SECTION HANDLING:
When a role has named sub-sections (e.g. "Chezuba AI Products", "Corporate Social Responsibility SaaS"), structure as:
- First item: {{ "title": "Founder & Chief Product Officer", "subtitle": "Chezuba", "dateRange": "Nov '17 - Dec '25", "location": "Mountain View, USA", "bullets": [] }}
- Sub-section items: {{ "title": "Chezuba AI Products", "isSubsection": true, "bullets": [...the bullets for that sub-section...] }}

ABSOLUTE RULES:
1. BULLET HANDLING: I have numbered every original bullet as [B1] through [B{len(bullet_lines)}]. For EVERY bullet you MUST do one of the following:
   a) DIRECTLY RELEVANT to the job description → Optimize it: reword to incorporate relevant keywords, skills, and terminology from the job description while preserving the original meaning, metrics, and achievements.
   b) NOT DIRECTLY RELEVANT to the job description → Soft Align it: rewrite the bullet to emphasize universal professional skills (leadership, efficiency, communication, problem-solving, stakeholder management, cross-functional collaboration, impact) rather than technical keywords. Keep the original facts, metrics, and meaning intact.
   For each bullet, include a "softAligned" array listing bullet numbers that were soft-aligned rather than directly optimized (e.g. "softAligned": ["B3", "B7"]).
2. TWO-PAGE LIMIT: The original resume has {experience_bullet_count} experience bullets (plus {skills_bullet_count} skills entries that will become label/value pairs, not bullets). A standard 2-page resume fits roughly 28-34 experience bullet points.
   {two_page_rule}
3. PRESERVE EXACTLY: name, contact info (including full URLs like https://www.linkedin.com/in/...), dates, company names, job titles, locations, education
4. OPTIMIZE: Make the job-relevant bullets shine the brightest with strong keyword alignment. Soft-aligned bullets should still read professionally but don't need heavy keyword insertion.
5. DO NOT FABRICATE: Never invent information, metrics, or claims not in the original
6. SECTION ORDER: Keep all sections in their original order
7. Use "experience" for work, "education" for education, "skills" for skills, "generic" for others
8. NEVER USE "N/A": If any field (dateRange, location, subtitle, etc.) has no data in the original resume, omit the field entirely or use an empty string "". Never output "N/A", "n/a", or similar placeholders.
9. NO TAGS OR REFERENCES IN OUTPUT: Do NOT include any bullet references like [B1], [optimized bullet for B1], [softAligned bullet for B7], or any bracketed annotations in your output text. The bullet numbers are for your reference only — the output must be clean, professional text with zero annotations or meta-commentary."""


def _build_call2_user_prompt(
    extracted_text: str,
    numbered_bullets: str,
    job_description: str,
    experience_bullet_count: int,
    needs_compression: bool,
    retry_attempt: int,
) -> str:
    retry_prefix = ""
    if retry_attempt > 0:
        retry_prefix = (
            f"IMPORTANT: Your previous response was incomplete — it only had a fraction of the bullets. "
            f"You MUST output ALL {experience_bullet_count} experience bullets and every section. "
            f"Do NOT skip or truncate.\n\n"
        )
    compression_note = (
        "— compress older/non-relevant roles to max 2 bullets to fit 2 pages"
        if needs_compression
        else "— include ALL bullets, do not skip any"
    )
    return (
        f"{retry_prefix}ORIGINAL RESUME:\n{extracted_text}\n\n"
        f"NUMBERED BULLETS (optimize relevant bullets with job keywords, soft-align non-relevant ones"
        f"{compression_note}):\n{numbered_bullets}\n\n"
        f"JOB DESCRIPTION:\n{job_description}"
    )


async def tailor_resume(extracted_text: str, job_description: str) -> dict:
    client = get_client()

    bullet_lines = _extract_bullets(extracted_text)
    original_bullet_count = len(bullet_lines)

    skills_bullet_count = sum(
        1 for b in bullet_lines if re.match(r"^[A-Za-z &]+:\s+.+,.+", b)
    )
    experience_bullet_count = len(bullet_lines) - skills_bullet_count
    needs_compression = experience_bullet_count > 34

    print(
        f"Bullet count: {len(bullet_lines)} total ({experience_bullet_count} experience, "
        f"{skills_bullet_count} skills), needs compression: {needs_compression}"
    )

    numbered_bullets = "\n".join(f"[B{i+1}] {b}" for i, b in enumerate(bullet_lines))

    # ── CALL 1: Analyze ──────────────────────────────────────────────────────
    analysis_response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert resume analyst. Compare the resume against the job description "
                    "and return a brief JSON analysis. Do NOT rewrite or modify the resume — only analyze it.\n\n"
                    "Return valid JSON with exactly these fields:\n"
                    "1. \"missingSkills\": array of up to 8 skill strings from the job description that are missing from the resume\n"
                    "2. \"suggestedChanges\": array of up to 5 objects with {original, improved, reason} showing the most impactful bullet point improvements you'd recommend\n"
                    "3. \"overallScore\": integer 0-100 indicating resume-to-job match quality\n\n"
                    "Keep the response concise. Do not include any other fields."
                ),
            },
            {
                "role": "user",
                "content": f"Resume:\n{extracted_text}\n\nJob Description:\n{job_description}",
            },
        ],
        response_format={"type": "json_object"},
        max_tokens=3000,
    )
    analysis = json.loads(analysis_response.choices[0].message.content or "{}")
    print(f"Call 1 (analysis) complete — score: {analysis.get('overallScore')}")

    # ── CALL 2: Generate structured resume (with retry) ──────────────────────
    expected_min_bullets = (
        max(int(experience_bullet_count * 0.4), 8)
        if needs_compression
        else int(experience_bullet_count * 0.7)
    )

    structured_resume: dict = {}
    MAX_RETRIES = 2

    for attempt in range(MAX_RETRIES + 1):
        structure_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": _build_call2_system_prompt(
                        bullet_lines,
                        experience_bullet_count,
                        skills_bullet_count,
                        needs_compression,
                        attempt,
                    ),
                },
                {
                    "role": "user",
                    "content": _build_call2_user_prompt(
                        extracted_text,
                        numbered_bullets,
                        job_description,
                        experience_bullet_count,
                        needs_compression,
                        attempt,
                    ),
                },
            ],
            response_format={"type": "json_object"},
            max_tokens=10000,
        )
        structured_resume = json.loads(structure_response.choices[0].message.content or "{}")

        attempt_bullet_count = 0
        attempt_section_count = 0
        for section in structured_resume.get("sections", []):
            attempt_section_count += 1
            for item in section.get("items", []):
                if item.get("bullets"):
                    attempt_bullet_count += len(item["bullets"])

        print(
            f"Call 2 attempt {attempt + 1} — sections: {attempt_section_count}, "
            f"bullets: {attempt_bullet_count}/{experience_bullet_count}, "
            f"min required: {expected_min_bullets}"
        )

        if attempt_bullet_count >= expected_min_bullets and attempt_section_count >= 2:
            break

        if attempt < MAX_RETRIES:
            print(
                f"Call 2 output too sparse ({attempt_bullet_count} bullets, "
                f"{attempt_section_count} sections). Retrying (attempt {attempt + 2}/{MAX_RETRIES + 1})..."
            )
        else:
            print(f"Call 2 still sparse after {MAX_RETRIES + 1} attempts. Proceeding with best result.")

    # ── Post-process structured resume ───────────────────────────────────────
    output_bullet_count = 0
    for section in structured_resume.get("sections", []):
        section["title"] = _clean_na(section.get("title"))
        for item in section.get("items", []):
            item["title"] = _clean_na(item.get("title"))
            item["subtitle"] = _clean_na(item.get("subtitle"))
            item["dateRange"] = _clean_na(item.get("dateRange"))
            item["location"] = _clean_na(item.get("location"))
            item["label"] = _clean_na(item.get("label"))
            if item.get("bullets"):
                item["bullets"] = [
                    cleaned
                    for b in item["bullets"]
                    if (cleaned := _strip_tags(b))
                ]
                output_bullet_count += len(item["bullets"])

    if structured_resume.get("contactInfo"):
        structured_resume["contactInfo"] = _clean_na(structured_resume["contactInfo"])
    if structured_resume.get("summary"):
        structured_resume["summary"] = _strip_tags(structured_resume["summary"])

    print(
        f"Call 2 (structure) complete — name: {structured_resume.get('name')}, "
        f"sections: {len(structured_resume.get('sections', []))}, "
        f"bullets: {output_bullet_count}/{original_bullet_count}"
    )

    soft_aligned: list[str] = structured_resume.pop("softAligned", [])
    removed_bullets: list[dict] = structured_resume.pop("removedBullets", [])

    removed_indices: set[int] = set()
    for rb in removed_bullets:
        m = re.search(r"B(\d+)", rb.get("bullet", ""))
        if m:
            removed_indices.add(int(m.group(1)) - 1)

    print(f"Soft-aligned bullets: {len(soft_aligned)}, removed (two-page overflow only): {len(removed_bullets)}")

    # ── Derive top_changes ────────────────────────────────────────────────────
    optimized_bullets: list[str] = []
    for section in structured_resume.get("sections", []):
        for item in section.get("items", []):
            for b in item.get("bullets", []):
                optimized_bullets.append(b)

    kept_originals = [b for i, b in enumerate(bullet_lines) if i not in removed_indices]
    real_changes: list[dict] = []
    min_len = min(len(kept_originals), len(optimized_bullets))
    for i in range(min_len):
        orig = kept_originals[i].strip()
        improved = optimized_bullets[i].strip()
        if orig.lower() != improved.lower():
            real_changes.append({"original": orig, "improved": improved, "reason": ""})

    for rb in removed_bullets:
        m = re.search(r"B(\d+)", rb.get("bullet", ""))
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(bullet_lines):
                real_changes.append({
                    "original": bullet_lines[idx].strip(),
                    "improved": "[Removed — two-page limit]",
                    "reason": rb.get("reason") or "Removed to fit within two-page limit",
                })

    top_changes = real_changes[:8]

    # ── CALL 3: Generate reasons ─────────────────────────────────────────────
    if top_changes:
        try:
            changes_for_reasons = "\n".join(
                f'{i + 1}. Original: "{c["original"]}"\n   Improved: "{c["improved"]}"'
                for i, c in enumerate(top_changes)
            )
            reasons_response = await client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You explain resume optimization changes. For each before/after pair, write a brief, "
                            "specific reason (1 sentence, max 20 words) explaining why the change improves the "
                            'resume for the target job. Return JSON: {"reasons": ["reason1", "reason2", ...]}'
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Job Description: {job_description}\n\nChanges:\n{changes_for_reasons}",
                    },
                ],
                response_format={"type": "json_object"},
                max_tokens=500,
            )
            reasons_data = json.loads(reasons_response.choices[0].message.content or "{}")
            reasons: list[str] = reasons_data.get("reasons", [])
            for i, change in enumerate(top_changes):
                change["reason"] = reasons[i] if i < len(reasons) else "Improved alignment with job requirements"
        except Exception as e:
            print(f"Reason generation failed, using fallbacks: {e}")
            for change in top_changes:
                if not change["reason"]:
                    change["reason"] = "Improved alignment with job requirements"

    analysis["suggestedChanges"] = top_changes
    analysis["structuredResume"] = structured_resume
    analysis["fullOptimizedText"] = _build_full_optimized_text(structured_resume)

    return {
        "analysis": analysis,
        "structured_resume": structured_resume,
        "full_optimized_text": analysis["fullOptimizedText"],
    }
# In tailor.py
def generate_tailored_resume(job_description, original_resume_text, output_path):
    # ... your AI logic to get tailored_content ...
    
    # When creating the PDF:
    # pdf.output(output_path) 
    print(f"📄 PDF successfully generated at: {output_path}")