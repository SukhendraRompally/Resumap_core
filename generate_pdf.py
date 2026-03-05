import sys
import json
import re
import os
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import HexColor
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, HRFlowable, Table, TableStyle
)
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")

_TAG_RE = re.compile(r'\s*\[[^\]]*B\d+[^\]]*\]\s*\.?\s*')
_SOFT_ALIGNED_RE = re.compile(r'^\s*softAligned\s*:\s*\[.*?\]\s*$', re.IGNORECASE)
def strip_tags(s):
    if not s:
        return s
    s = _TAG_RE.sub('', s)
    s = _SOFT_ALIGNED_RE.sub('', s)
    return s.strip()

pdfmetrics.registerFont(TTFont("Inter", os.path.join(FONT_DIR, "Inter-Regular.ttf")))
pdfmetrics.registerFont(TTFont("Inter-Bold", os.path.join(FONT_DIR, "Inter-Bold.ttf")))
pdfmetrics.registerFont(TTFont("Inter-Italic", os.path.join(FONT_DIR, "Inter-Italic.ttf")))
pdfmetrics.registerFont(TTFont("Inter-BoldItalic", os.path.join(FONT_DIR, "Inter-BoldItalic.ttf")))

pdfmetrics.registerFontFamily(
    "Inter",
    normal="Inter",
    bold="Inter-Bold",
    italic="Inter-Italic",
    boldItalic="Inter-BoldItalic",
)

COLOR_PRIMARY = HexColor("#1a1a1a")
COLOR_HEADING = HexColor("#111111")
COLOR_ACCENT = HexColor("#2563eb")
COLOR_MUTED = HexColor("#555555")
COLOR_DIVIDER = HexColor("#d1d5db")

FONT_BODY = "Inter"
FONT_BOLD = "Inter-Bold"
FONT_ITALIC = "Inter-Italic"

STYLES = {
    "name": ParagraphStyle(
        "Name",
        fontName=FONT_BOLD,
        fontSize=18,
        leading=22,
        textColor=COLOR_HEADING,
        alignment=TA_CENTER,
        spaceAfter=2,
    ),
    "contact": ParagraphStyle(
        "Contact",
        fontName=FONT_BODY,
        fontSize=9,
        leading=13,
        textColor=COLOR_MUTED,
        alignment=TA_CENTER,
        spaceAfter=4,
    ),
    "section_heading": ParagraphStyle(
        "SectionHeading",
        fontName=FONT_BOLD,
        fontSize=11.5,
        leading=15,
        textColor=COLOR_ACCENT,
        spaceBefore=10,
        spaceAfter=5,
        textTransform="uppercase",
    ),
    "company_name": ParagraphStyle(
        "CompanyName",
        fontName=FONT_BOLD,
        fontSize=10,
        leading=13,
        textColor=COLOR_HEADING,
        spaceBefore=6,
        spaceAfter=1,
    ),
    "job_title": ParagraphStyle(
        "JobTitle",
        fontName=FONT_BOLD,
        fontSize=9.5,
        leading=13,
        textColor=COLOR_PRIMARY,
        spaceAfter=1,
    ),
    "job_meta": ParagraphStyle(
        "JobMeta",
        fontName=FONT_ITALIC,
        fontSize=9,
        leading=12,
        textColor=COLOR_MUTED,
        spaceAfter=3,
    ),
    "bullet": ParagraphStyle(
        "Bullet",
        fontName=FONT_BODY,
        fontSize=9.5,
        leading=13,
        textColor=COLOR_PRIMARY,
        leftIndent=14,
        bulletIndent=0,
        spaceAfter=2,
    ),
    "body": ParagraphStyle(
        "Body",
        fontName=FONT_BODY,
        fontSize=9.5,
        leading=13,
        textColor=COLOR_PRIMARY,
        spaceAfter=2,
    ),
    "skills_label": ParagraphStyle(
        "SkillsLabel",
        fontName=FONT_BOLD,
        fontSize=9.5,
        leading=13,
        textColor=COLOR_PRIMARY,
    ),
    "skills_value": ParagraphStyle(
        "SkillsValue",
        fontName=FONT_BODY,
        fontSize=9.5,
        leading=13,
        textColor=COLOR_PRIMARY,
    ),
}


URL_PATTERN = re.compile(r'(https?://[^\s,|]+)')


def escape_xml(text):
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def linkify(text):
    parts = URL_PATTERN.split(text)
    result = ""
    for i, part in enumerate(parts):
        if URL_PATTERN.match(part):
            display = part
            if "linkedin.com" in part:
                display = re.sub(r'https?://(www\.)?', '', part).rstrip('/')
            result += f'<a href="{escape_xml(part)}" color="#2563eb">{escape_xml(display)}</a>'
        else:
            result += escape_xml(part)
    return result


def _col_widths(doc, right_text, font_name=None, font_size=9, max_right_pct=0.40):
    if font_name is None:
        font_name = FONT_ITALIC
    avail = doc.pagesize[0] - doc.leftMargin - doc.rightMargin
    right_w = stringWidth(right_text, font_name, font_size) + 8
    right_col = min(right_w, avail * max_right_pct)
    left_col = avail - right_col
    return [left_col, right_col]


def generate_pdf(output_path, structured_resume):
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.65 * inch,
        rightMargin=0.65 * inch,
        topMargin=0.5 * inch,
        bottomMargin=0.5 * inch,
    )

    story = []

    name = structured_resume.get("name", "")
    contact_info = structured_resume.get("contactInfo", "")
    summary = strip_tags(structured_resume.get("summary", ""))
    sections = structured_resume.get("sections", [])

    if name:
        story.append(Paragraph(escape_xml(name), STYLES["name"]))

    if contact_info:
        story.append(Paragraph(linkify(contact_info), STYLES["contact"]))

    if name or contact_info:
        story.append(Spacer(1, 4))
        story.append(HRFlowable(width="100%", thickness=1, color=COLOR_DIVIDER, spaceAfter=6))

    if summary:
        story.append(Paragraph(
            '<font name="Inter-Bold" color="#2563eb" size="11">PROFESSIONAL SUMMARY</font>',
            STYLES["section_heading"]
        ))
        story.append(Paragraph(escape_xml(summary), STYLES["body"]))

    for section in sections:
        section_title = section.get("title", "")
        section_type = section.get("type", "generic")
        items = section.get("items", [])

        story.append(Spacer(1, 4))
        story.append(HRFlowable(width="100%", thickness=0.5, color=COLOR_DIVIDER, spaceAfter=2))
        story.append(Paragraph(
            f'<font name="Inter-Bold" color="#2563eb" size="11">{escape_xml(section_title.upper())}</font>',
            STYLES["section_heading"]
        ))

        if section_type == "experience":
            is_first_item = True
            for item in items:
                title = item.get("title", "")
                subtitle = item.get("subtitle", "")
                date_range = item.get("dateRange", "")
                location = item.get("location", "")
                bullets = item.get("bullets", [])
                is_subsection = item.get("isSubsection", False)

                meta_right = ""
                if date_range:
                    meta_right = date_range
                if location:
                    if meta_right:
                        meta_right += f" | {location}"
                    else:
                        meta_right = location

                if is_subsection or (not date_range and not location and not subtitle):
                    if title:
                        story.append(Spacer(1, 3))
                        story.append(Paragraph(
                            f'<font name="Inter-Bold" size="9.5">{escape_xml(title)}</font>',
                            ParagraphStyle("SubSection", fontName=FONT_BOLD, fontSize=9.5, leading=13, textColor=COLOR_HEADING, spaceAfter=2)
                        ))
                elif title or subtitle:
                    if not is_first_item:
                        story.append(Spacer(1, 8))
                    title_line = f'<font name="Inter-Bold" size="10">{escape_xml(title)}</font>'
                    if subtitle:
                        title_line += f'<font name="Inter" size="10" color="#555555">  |  {escape_xml(subtitle)}</font>'
                    if meta_right:
                        header_data = [
                            [
                                Paragraph(title_line, STYLES["company_name"]),
                                Paragraph(
                                    f'<font name="Inter-Italic" color="#555555" size="9">{escape_xml(meta_right)}</font>',
                                    ParagraphStyle("Right", fontName=FONT_ITALIC, fontSize=9, alignment=2, textColor=COLOR_MUTED, spaceBefore=6)
                                )
                            ]
                        ]
                        header_table = Table(header_data, colWidths=_col_widths(doc, meta_right))
                        header_table.setStyle(TableStyle([
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                        ]))
                        story.append(header_table)
                    else:
                        story.append(Paragraph(title_line, STYLES["company_name"]))

                    is_first_item = False

                for bullet in bullets:
                    bullet_text = strip_tags(bullet)
                    if bullet_text:
                        story.append(Paragraph(
                            f'\u2022  {escape_xml(bullet_text)}',
                            STYLES["bullet"]
                        ))

        elif section_type == "education":
            for item in items:
                title = item.get("title", "")
                subtitle = item.get("subtitle", "")
                date_range = item.get("dateRange", "")
                bullets = item.get("bullets", [])

                if title:
                    title_line = escape_xml(title)
                    if date_range:
                        header_data = [
                            [
                                Paragraph(title_line, STYLES["company_name"]),
                                Paragraph(
                                    f'<font name="Inter-Italic" color="#555555" size="9">{escape_xml(date_range)}</font>',
                                    ParagraphStyle("Right", fontName=FONT_ITALIC, fontSize=9, alignment=2, textColor=COLOR_MUTED, spaceBefore=6)
                                )
                            ]
                        ]
                        header_table = Table(header_data, colWidths=_col_widths(doc, date_range))
                        header_table.setStyle(TableStyle([
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                        ]))
                        story.append(header_table)
                    else:
                        story.append(Paragraph(title_line, STYLES["company_name"]))

                if subtitle:
                    story.append(Paragraph(escape_xml(subtitle), STYLES["job_meta"]))

                for bullet in bullets:
                    bt = strip_tags(bullet)
                    if bt:
                        story.append(Paragraph(
                            f'\u2022  {escape_xml(bt)}',
                            STYLES["bullet"]
                        ))

                story.append(Spacer(1, 3))

        elif section_type == "skills":
            for item in items:
                label = item.get("label", "")
                value = item.get("value", "")
                if label and value:
                    story.append(Paragraph(
                        f'<font name="Inter-Bold">{escape_xml(label)}:</font>  {escape_xml(value)}',
                        STYLES["body"]
                    ))
                elif value:
                    story.append(Paragraph(escape_xml(value), STYLES["body"]))

        else:
            for item in items:
                title = item.get("title", "")
                subtitle = item.get("subtitle", "")
                date_range = item.get("dateRange", "")
                bullets = item.get("bullets", [])
                value = item.get("value", "")

                if title:
                    if date_range:
                        header_data = [
                            [
                                Paragraph(escape_xml(title), STYLES["job_title"]),
                                Paragraph(
                                    f'<font name="Inter-Italic" color="#555555" size="9">{escape_xml(date_range)}</font>',
                                    ParagraphStyle("Right", fontName=FONT_ITALIC, fontSize=9, alignment=2, textColor=COLOR_MUTED)
                                )
                            ]
                        ]
                        header_table = Table(header_data, colWidths=_col_widths(doc, date_range))
                        header_table.setStyle(TableStyle([
                            ("VALIGN", (0, 0), (-1, -1), "TOP"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 0),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                            ("TOPPADDING", (0, 0), (-1, -1), 0),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                        ]))
                        story.append(header_table)
                    else:
                        story.append(Paragraph(escape_xml(title), STYLES["job_title"]))

                if subtitle:
                    story.append(Paragraph(escape_xml(subtitle), STYLES["job_meta"]))

                for bullet in bullets:
                    bt = strip_tags(bullet)
                    if bt:
                        story.append(Paragraph(
                            f'\u2022  {escape_xml(bt)}',
                            STYLES["bullet"]
                        ))

                if value:
                    story.append(Paragraph(escape_xml(value), STYLES["body"]))

                story.append(Spacer(1, 3))

    doc.build(story)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: generate_pdf.py <output_path>"}))
        sys.exit(1)

    output_path = sys.argv[1]
    json_data = json.loads(sys.stdin.read())

    structured_resume = json_data.get("structuredResume", {})

    try:
        generate_pdf(output_path, structured_resume)
        print(json.dumps({"success": True, "path": output_path}))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
