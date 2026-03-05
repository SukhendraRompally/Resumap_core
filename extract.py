import sys
import json
import re
import pdfplumber

try:
    import fitz
    HAS_FITZ = True
except ImportError:
    HAS_FITZ = False


def extract_hyperlinks(pdf_path):
    if not HAS_FITZ:
        return {}

    link_map = {}
    try:
        doc = fitz.open(pdf_path)
        for page in doc:
            for link in page.get_links():
                uri = link.get("uri", "")
                if not uri:
                    continue
                from_rect = link.get("from")
                if from_rect:
                    text_at = page.get_text("text", clip=fitz.Rect(from_rect)).strip()
                    text_at = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text_at).strip()
                    if text_at and text_at.lower() != uri.lower():
                        link_map[text_at] = uri
        doc.close()
    except Exception:
        pass
    return link_map


def replace_link_text(text, link_map):
    for label, url in link_map.items():
        if label in text:
            text = text.replace(label, f"{label} ({url})")
    return text


def extract_text_from_pdf(pdf_path):
    try:
        text_content = []
        layout_data = []

        link_map = extract_hyperlinks(pdf_path)

        with pdfplumber.open(pdf_path) as pdf:
            for page_num, page in enumerate(pdf.pages):
                text_content.append(page.extract_text() or "")

                page_layout = {
                    "page": page_num,
                    "width": float(page.width),
                    "height": float(page.height),
                    "lines": []
                }

                chars = page.chars
                if not chars:
                    layout_data.append(page_layout)
                    continue

                current_line = []
                current_top = None
                tolerance = 2

                sorted_chars = sorted(chars, key=lambda c: (round(float(c["top"]) / tolerance) * tolerance, float(c["x0"])))

                for char in sorted_chars:
                    char_top = float(char["top"])
                    if current_top is None or abs(char_top - current_top) > tolerance:
                        if current_line:
                            page_layout["lines"].append(_process_line(current_line))
                        current_line = [char]
                        current_top = char_top
                    else:
                        current_line.append(char)

                if current_line:
                    page_layout["lines"].append(_process_line(current_line))

                layout_data.append(page_layout)

        full_text = "\n".join(text_content)

        if link_map:
            full_text = replace_link_text(full_text, link_map)

        hyperlinks = []
        for label, url in link_map.items():
            hyperlinks.append({"text": label, "url": url})

        return {"text": full_text, "layout": layout_data, "hyperlinks": hyperlinks}
    except Exception as e:
        return {"error": str(e)}


def _process_line(chars):
    text = ""
    prev_x1 = None
    for c in chars:
        if prev_x1 is not None:
            gap = float(c["x0"]) - prev_x1
            if gap > 3:
                text += " "
        text += c["text"]
        prev_x1 = float(c["x1"])

    sizes = [float(c.get("size", 10)) for c in chars]
    avg_size = sum(sizes) / len(sizes) if sizes else 10

    fonts = [c.get("fontname", "") for c in chars]
    is_bold = any("Bold" in f or "bold" in f for f in fonts)

    return {
        "text": text.strip(),
        "x": float(chars[0]["x0"]),
        "top": float(chars[0]["top"]),
        "size": round(avg_size, 1),
        "bold": is_bold,
        "font": fonts[0] if fonts else ""
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No file path provided"}))
        sys.exit(1)

    pdf_path = sys.argv[1]
    result = extract_text_from_pdf(pdf_path)
    print(json.dumps(result))
