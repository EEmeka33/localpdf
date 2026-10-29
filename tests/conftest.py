"""Generates realistic sample inputs once per test session."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz
import pytest
from PIL import Image, ImageDraw

LOREM = ("Local processing keeps documents private. The quarterly report shows revenue growth in every region. "
         "Contact alice@example.com or call +1 555 123 4567 for details. Margins improved because costs fell. "
         "The board approved the new budget for research and development. Customers praised the faster delivery. ")


def _png(color=(30, 95, 209), size=(400, 260), text="IMG") -> bytes:
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, size[0] - 20, size[1] - 20], outline="white", width=6)
    d.text((40, 40), text, fill="white")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture(scope="session")
def samples(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    s: dict[str, Path] = {}

    # 1) a 6-page text PDF with headings, a table, an image and bookmarks
    doc = fitz.open()
    for i in range(6):
        page = doc.new_page()
        page.insert_text((72, 80), f"Chapter {i + 1}: Results", fontsize=22, fontname="hebo")
        page.insert_textbox(fitz.Rect(72, 110, 520, 400), LOREM * 2, fontsize=11)
        if i == 1:  # a ruled table
            x0, y0, cw, rh = 72, 420, 140, 22
            rows = [["Region", "Q1", "Q2"], ["North", "1200", "1350"], ["South", "980", "1105"], ["West", "1,500", "1,620"]]
            for r, row in enumerate(rows):
                for c, val in enumerate(row):
                    rect = fitz.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh)
                    page.draw_rect(rect, color=(0, 0, 0), width=0.8)
                    page.insert_text((rect.x0 + 5, rect.y1 - 7), val, fontsize=10)
        if i == 2:
            page.insert_image(fitz.Rect(72, 450, 312, 606), stream=_png())
    doc.set_toc([[1, f"Chapter {i + 1}", i + 1] for i in range(6)])
    doc.set_metadata({"title": "Sample report", "author": "Test Author"})
    s["pdf"] = d / "report.pdf"
    doc.save(s["pdf"])

    # 2) a second version with one change (for compare) and a short PDF
    doc = fitz.open(s["pdf"])
    page = doc[0]
    page.add_redact_annot(page.search_for("revenue")[0])
    page.apply_redactions()
    page.insert_text((72, 760), "Added line in version two", fontsize=12)
    s["pdf2"] = d / "report_v2.pdf"
    doc.save(s["pdf2"])

    doc = fitz.open()
    doc.new_page().insert_text((72, 100), "Short document", fontsize=14)
    doc.new_page()  # blank page
    doc.new_page().insert_text((72, 100), "Third page", fontsize=14)
    s["short"] = d / "short.pdf"
    doc.save(s["short"])

    # 3) a "scanned" PDF – text only as pixels
    src = fitz.open()
    p = src.new_page()
    p.insert_text((72, 120), "Scanned invoice number 12345", fontsize=26)
    pix = p.get_pixmap(dpi=200)
    scan = fitz.open()
    sp = scan.new_page(width=p.rect.width, height=p.rect.height)
    sp.insert_image(sp.rect, pixmap=pix)
    s["scanned"] = d / "scanned.pdf"
    scan.save(s["scanned"])

    # 4) a form
    doc = fitz.open()
    page = doc.new_page()
    w = fitz.Widget()
    w.field_name, w.field_type, w.rect = "name", fitz.PDF_WIDGET_TYPE_TEXT, fitz.Rect(72, 72, 300, 92)
    page.add_widget(w)
    cb = fitz.Widget()
    cb.field_name, cb.field_type, cb.rect = "agree", fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.Rect(72, 110, 90, 128)
    page.add_widget(cb)
    s["form"] = d / "form.pdf"
    doc.save(s["form"])

    # 5) images
    for name, col in (("photo1.jpg", (200, 60, 50)), ("photo2.png", (40, 150, 80))):
        path = d / name
        Image.open(io.BytesIO(_png(col, (800, 1100), name))).save(path)
        s[name] = path
    sig = Image.new("RGB", (500, 160), "white")
    ImageDraw.Draw(sig).line([(20, 120), (120, 30), (220, 130), (320, 40), (470, 110)], fill=(10, 20, 90), width=8)
    s["signature"] = d / "signature.png"
    sig.save(s["signature"])

    # 6) HTML site with CSS + image, Markdown, text
    site = d / "site"
    (site / "img").mkdir(parents=True)
    (site / "img" / "logo.png").write_bytes(_png((229, 50, 45), (200, 80), "LOGO"))
    (site / "style.css").write_text("body{font-family:sans-serif;color:#223} h1{color:#e5322d} .box{border:2px solid #1f5fd1;padding:12px}")
    (site / "index.html").write_text(
        "<!doctype html><html><head><link rel='stylesheet' href='style.css'><title>Site</title></head><body>"
        "<h1>Hello from a local HTML file</h1><img src='img/logo.png'><div class='box'>Styled box</div>"
        "<p id='js'></p><script>document.getElementById('js').textContent='JavaScript ran';</script></body></html>")
    s["html"] = site / "index.html"
    s["md"] = d / "notes.md"
    s["md"].write_text("# Notes\n\nSome **bold** text.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```python\nprint('hi')\n```\n")
    s["txt"] = d / "log.txt"
    s["txt"].write_text("\n".join(f"line {i}: something happened" for i in range(200)))

    # 7) office files
    from docx import Document
    from openpyxl import Workbook
    from pptx import Presentation

    docx = Document()
    docx.add_heading("Word document", 0)
    docx.add_paragraph("Paragraph converted by LibreOffice.")
    s["docx"] = d / "letter.docx"
    docx.save(s["docx"])
    wb = Workbook()
    wb.active.append(["Item", "Qty"])
    wb.active.append(["Apples", 3])
    s["xlsx"] = d / "sheet.xlsx"
    wb.save(s["xlsx"])
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "Slide title"
    s["pptx"] = d / "deck.pptx"
    prs.save(s["pptx"])
    return s
