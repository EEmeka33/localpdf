"""Run every conversion on the sample documents, score the results and render previews.

    python examples/make_inputs.py      # (re)create the synthetic inputs
    python examples/run_examples.py     # → examples/outputs/, examples/previews/, examples/RESULTS.md

Metrics (all automatic – the human evaluation lives in examples/README.md):
  text recall   share of the source words that are present in the output (order-insensitive)
  visual sim.   1 - mean pixel difference between source and output page renders (round trips only)
  cell recall   share of the numbers printed in the PDF that became numeric spreadsheet cells
  OCR accuracy  character similarity between the OCR text layer and the ground truth
"""
from __future__ import annotations

import difflib
import html as html_lib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFont, ImageOps

HERE = Path(__file__).parent
IN, OUT, PREV = HERE / "inputs", HERE / "outputs", HERE / "previews"
RW = IN / "realworld"
sys.path.insert(0, str(HERE.parent))

from localpdf.registry import get  # noqa: E402
from localpdf.runner import run_tool  # noqa: E402

# --------------------------------------------------------------------------- text extraction
WORD = re.compile(r"[^\W_]{2,}", re.UNICODE)


def words(text: str) -> Counter:
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text or "")  # "3,873.52" and 3873.52 tokenize alike
    return Counter(w.lower() for w in WORD.findall(text))


def recall(ref: str, out: str) -> float:
    r, o = words(ref), words(out)
    total = sum(r.values())
    return sum((r & o).values()) / total if total else 0.0


def text_of(path: Path) -> str:
    """Plain text of any document type we produce or consume."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "\n".join(p.get_text() for p in fitz.open(path))
    if ext == ".docx":
        from docx import Document

        d = Document(path)
        parts = [p.text for p in d.paragraphs]
        parts += [c.text for t in d.tables for row in t.rows for c in row.cells]
        for s in d.sections:
            parts += [p.text for p in s.header.paragraphs] + [p.text for p in s.footer.paragraphs]
        return "\n".join(parts)
    if ext == ".pptx":
        from pptx import Presentation

        out = []
        for slide in Presentation(path).slides:
            for sh in slide.shapes:
                if sh.has_text_frame:
                    out.append(sh.text_frame.text)
                if getattr(sh, "has_table", False) and sh.has_table:
                    out += [c.text for r in sh.table.rows for c in r.cells]
            if slide.has_notes_slide:
                out.append(slide.notes_slide.notes_text_frame.text)
        return "\n".join(out)
    if ext == ".xlsx":
        from openpyxl import load_workbook

        wb = load_workbook(path, data_only=True)
        return "\n".join(" ".join(str(c) for c in row if c is not None) for ws in wb for row in ws.iter_rows(values_only=True))
    if ext in (".html", ".htm"):
        raw = path.read_text(encoding="utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", raw)
        return html_lib.unescape(re.sub(r"<[^>]+>", " ", raw))
    if ext == ".rtf":
        raw = path.read_text(errors="replace")
        return re.sub(r"\\[a-z]+-?\d* ?|[{}]", " ", re.sub(r"\{\\fonttbl.*?\}\}|\{\\colortbl.*?\}", "", raw))
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- rendering for previews
def soffice_pdf(path: Path) -> Path:
    tmp = Path(tempfile.mkdtemp())
    subprocess.run(["soffice", f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--convert-to", "pdf",
                    "--outdir", str(tmp), str(path)], capture_output=True, timeout=300)
    return tmp / (path.stem + ".pdf")


def render(path: Path, page=0, width=520) -> Image.Image:
    """First page (or image) of anything, as a thumbnail."""
    ext = path.suffix.lower()
    if path.is_dir():
        path = path / "index.html"
        ext = ".html"
    if ext in (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif"):
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    elif ext in (".html", ".htm"):
        img = browser_screenshot(path)
    elif ext == ".pdf":
        doc = fitz.open(path)
        pix = doc[min(page, doc.page_count - 1)].get_pixmap(dpi=110)
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    elif ext == ".xlsx":  # print all columns on the page width (LibreOffice's default would cut them)
        tmp = Path(tempfile.mkdtemp()) / (path.stem + ".pdf")
        run_tool(get("excel-to-pdf"), [str(path)], tmp, {"fit": "width"})
        return render(tmp, page, width)
    elif ext in (".docx", ".pptx", ".csv", ".rtf", ".odt"):
        return render(soffice_pdf(path), page, width)
    else:  # text-like: show the first lines
        img = Image.new("RGB", (900, 1100), "white")
        d = ImageDraw.Draw(img)
        fnt = _font(15)
        for i, line in enumerate(path.read_text(errors="replace").splitlines()[:52]):
            d.text((18, 14 + i * 20), line[:95], fill="black", font=fnt)
    img.thumbnail((width, int(width * 1.6)))
    return img


def browser_screenshot(path: Path) -> Image.Image:
    import io
    import os

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        exe = os.environ.get("LOCALPDF_CHROMIUM")
        b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1100, "height": 1400})
        pg.goto(path.resolve().as_uri())
        pg.wait_for_timeout(300)
        png = pg.screenshot(full_page=False)
        b.close()
    return Image.open(io.BytesIO(png)).convert("RGB")


def _font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def preview(job_id: str, title: str, left: Path, right: Path, right_page=0):
    a, b = render(left), render(right, right_page)
    pad, head = 24, 56
    w = a.width + b.width + pad * 3
    h = max(a.height, b.height) + head + pad
    sheet = Image.new("RGB", (w, h), "#eceef3")
    d = ImageDraw.Draw(sheet)
    d.text((pad, 16), title, fill="#1d2433", font=_font(20))
    d.text((pad, head - 18), "INPUT", fill="#6b6b78", font=_font(13))
    d.text((pad * 2 + a.width, head - 18), "OUTPUT", fill="#6b6b78", font=_font(13))
    sheet.paste(a, (pad, head))
    sheet.paste(b, (pad * 2 + a.width, head))
    PREV.mkdir(exist_ok=True)
    sheet.save(PREV / f"{job_id}.jpg", quality=85, optimize=True)


def visual_similarity(src_pdf: Path, out_pdf: Path, pages=3) -> float:
    s, o = fitz.open(src_pdf), fitz.open(out_pdf)
    scores = []
    for i in range(min(pages, s.page_count, o.page_count)):
        imgs = []
        for doc in (s, o):
            pix = doc[i].get_pixmap(dpi=40, colorspace=fitz.csGRAY)
            imgs.append(Image.frombytes("L", (pix.width, pix.height), pix.samples).resize((200, 260)))
        diff = sum(abs(x - y) for x, y in zip(imgs[0].tobytes(), imgs[1].tobytes())) / (200 * 260 * 255)
        scores.append(1 - diff)
    return sum(scores) / len(scores) if scores else 0.0


# --------------------------------------------------------------------------- jobs
JOBS = [
    # id, tool, inputs, options, output name, kind, description
    ("01-word-to-pdf", "word-to-pdf", ["report.docx"], {}, "report.pdf", "to", "3-page Word report: header/footer, lists, styled table, chart image"),
    ("02-excel-to-pdf", "excel-to-pdf", ["invoices.xlsx"], {}, "invoices.pdf", "to", "2 sheets, formulas, € formats, merged header, bar chart"),
    ("03-csv-to-pdf", "excel-to-pdf", ["weather.csv"], {}, "weather.pdf", "to", "plain CSV"),
    ("04-powerpoint-to-pdf", "powerpoint-to-pdf", ["pitch.pptx"], {}, "pitch.pdf", "to", "16:9 deck: native chart, table, shapes, picture"),
    ("05-html-folder-to-pdf", "html-to-pdf", ["website"], {}, "website-a4.pdf", "to", "local website folder (CSS grid, SVG, JS canvas), Chromium, A4 print"),
    ("06-html-single-page", "html-to-pdf", ["website"], {"paper": "single", "media": "screen"}, "website-single.pdf", "to", "same site as one long page, screen CSS"),
    ("07-html-weasyprint", "html-to-pdf", ["website"], {"engine": "weasyprint"}, "website-weasyprint.pdf", "to", "same site with the no-browser fallback engine"),
    ("08-html-invoice", "html-to-pdf", ["invoice.html"], {}, "invoice.pdf", "to", "single .html with @page rules and a forced page break"),
    ("09-images-to-pdf", "jpg-to-pdf", ["landscape.webp", "landscape-small.jpg", "logo-transparent.png", "exif-rotated.jpg"],
     {"page_size": "a4", "margin": "small"}, "images.pdf", "to", "WEBP + JPG + transparent PNG + EXIF-rotated photo → A4"),
    ("10-tiff-fax-to-pdf", "jpg-to-pdf", ["fax-3-pages.tiff"], {}, "fax.pdf", "to", "multi-page Group-4 TIFF"),
    ("11-scan-photo-to-pdf", "scan-to-pdf", ["phone-photo-of-document.jpg"], {"mode": "bw", "ocr": True}, "scan.pdf", "to",
     "tilted, unevenly lit phone photo → clean B&W + OCR"),
    ("12-markdown-to-pdf", "markdown-to-pdf", ["README-sample.md"], {}, "readme.pdf", "to", "Markdown: table, code, task list, image"),
    ("13-text-to-pdf", "text-to-pdf", ["server.log"], {"line_numbers": True}, "server-log.pdf", "to", "260-line log file"),
    ("14-rtf-to-pdf", "office-to-pdf", ["letter.rtf"], {}, "letter.pdf", "to", "RTF letter via the generic LibreOffice converter"),
    ("15-pdf-to-jpg", "pdf-to-jpg", [RW / "federal-register-2020-17221.pdf"], {"pages": "1-2", "dpi": 110}, "federal-register-jpg", "from",
     "real 3-column Federal Register pages → JPG"),
    ("16-pdf-extract-images", "pdf-to-jpg", ["@01-word-to-pdf/report.pdf"], {"mode": "extract"}, "report-images", "from",
     "pull the embedded chart back out of the Word-made PDF"),
    ("17-pdf-to-word-realworld", "pdf-to-word", [RW / "federal-register-2020-17221.pdf"], {"pages": "1-3"}, "federal-register.docx", "from",
     "real multi-column government notice → DOCX"),
    ("18-pdf-to-word-roundtrip", "pdf-to-word", ["@01-word-to-pdf/report.pdf"], {}, "report-roundtrip.docx", "from",
     "round trip DOCX → PDF → DOCX"),
    ("19-pdf-to-pptx-image", "pdf-to-powerpoint", ["@04-powerpoint-to-pdf/pitch.pdf"], {"dpi": 150}, "pitch-image.pptx", "from",
     "round trip, image mode (pixel-exact, text in notes)"),
    ("20-pdf-to-pptx-editable", "pdf-to-powerpoint", ["@04-powerpoint-to-pdf/pitch.pdf"], {"mode": "editable"}, "pitch-editable.pptx", "from",
     "round trip, editable mode (LibreOffice import)"),
    ("21-pdf-to-excel-nics", "pdf-to-excel", [RW / "nics-background-checks-2015-11.pdf"], {}, "nics.xlsx", "from",
     "real FBI statistics table (55 rows × 25 columns)"),
    ("22-pdf-to-excel-senate", "pdf-to-excel", [RW / "senate-expenditures.pdf"], {}, "senate.xlsx", "from", "real Senate ledger, text-aligned columns"),
    ("23-pdf-to-excel-roundtrip", "pdf-to-excel", ["@02-excel-to-pdf/invoices.pdf"], {"layout": "single-sheet"}, "invoices-roundtrip.xlsx", "from",
     "round trip XLSX → PDF → XLSX"),
    ("24-pdf-to-pdfa", "pdf-to-pdfa", [RW / "scotus-transcript-p1.pdf"], {"level": "2b"}, "scotus-pdfa.pdf", "from", "Supreme Court transcript → PDF/A-2b"),
    ("25-pdf-to-text", "pdf-to-text", [RW / "federal-register-2020-17221.pdf"], {"pages": "1-2"}, "federal-register.txt", "from",
     "reading order on a 3-column page"),
    ("26-pdf-to-html", "pdf-to-html", [RW / "scotus-transcript-p1.pdf"], {}, "scotus.html", "from", "transcript → self-contained HTML"),
    ("27-pdf-to-markdown", "pdf-to-markdown", ["@01-word-to-pdf/report.pdf"], {}, "report.md", "from", "headings/lists recovered as Markdown"),
    ("28-ocr", "ocr", ["scanned-delivery-notes.pdf"], {}, "scanned-ocr.pdf", "from", "2 noisy, slightly rotated scanned pages → searchable"),
]


def resolve(item) -> Path:
    s = str(item)
    if s.startswith("@"):
        return OUT / s[1:]
    p = Path(item)
    return p if p.is_absolute() else IN / p


def source_text(path: Path, opts: dict) -> str:
    """Reference text of the input – only the pages the job converted."""
    if path.suffix.lower() == ".pdf" and opts.get("pages"):
        from localpdf.utils import parse_pages

        doc = fitz.open(path)
        return "\n".join(doc[i].get_text() for i in parse_pages(opts["pages"], doc.page_count))
    return text_of(path)


def score(job, inputs, produced) -> dict:
    jid, tool, opts = job[0], job[1], job[3]
    m: dict = {}
    main = produced[0]
    src = inputs[0]
    if tool == "pdf-to-jpg":
        m["files"] = len(produced)
        return m
    if tool == "ocr":
        truth = (IN / "scanned-delivery-notes.truth.txt").read_text()
        got = text_of(main)
        norm = lambda s: re.sub(r"\s+", " ", s).strip().lower()  # noqa: E731
        m["ocr_char_accuracy"] = difflib.SequenceMatcher(None, norm(truth), norm(got), autojunk=False).ratio()
        m["text_recall"] = recall(truth, got)
        return m
    if tool == "scan-to-pdf":
        m["searchable_words"] = sum(words(text_of(main)).values())
        return m
    if tool == "pdf-to-pdfa":
        import pikepdf

        with pikepdf.open(main) as pdf:
            xmp = str(pdf.open_metadata())
            m["pdfa_declared"] = "pdfaid" in xmp
            m["output_intent"] = "/OutputIntents" in pdf.Root
            fonts, embedded = 0, 0
            for page in pdf.pages:
                for f in (page.Resources.get("/Font") or {}).values():
                    fonts += 1
                    desc = f.get("/FontDescriptor") or (f.get("/DescendantFonts")[0].get("/FontDescriptor") if f.get("/DescendantFonts") else None)
                    embedded += bool(desc and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3")))
            m["fonts_embedded"] = f"{embedded}/{fonts}"
        m["text_recall"] = recall(source_text(src, opts), text_of(main))
        return m
    if tool == "pdf-to-excel":
        from openpyxl import load_workbook

        # every whole word of the PDF that is a number (dates, IDs, codes don't count) …
        num_word = re.compile(r"\(?-?[$€£]?\d{1,3}(?:,\d{3})*(?:\.\d+)?%?\)?|\(?-?[$€£]?\d+(?:\.\d+)?%?\)?")
        doc = fitz.open(src)
        expected = Counter()
        for page in doc:
            for w in page.get_text("words"):
                if num_word.fullmatch(w[4]) and not re.fullmatch(r"0\d+", w[4]):
                    v = float(re.sub(r"[^\d.]", "", w[4]))
                    expected[round(-v if w[4].startswith(("(", "-")) else v, 4)] += 1
        # … should come back as a numeric cell with the same value
        wb = load_workbook(main)
        got = Counter()
        for ws in wb:
            for row in ws.iter_rows():
                for c in row:
                    if isinstance(c.value, (int, float)) and not isinstance(c.value, bool):
                        v = c.value * 100 if str(c.number_format).endswith("%") else c.value
                        got[round(v, 4)] += 1
        total = sum(expected.values())
        m["numeric_cell_recall"] = sum((expected & got).values()) / total if total else 0
        m["sheets"] = len(wb.sheetnames)
        m["text_recall"] = recall(source_text(src, opts), text_of(main))
        return m
    if tool == "jpg-to-pdf":
        doc = fitz.open(main)
        m["pages"] = doc.page_count
        m["page_sizes"] = sorted({f"{round(p.rect.width)}x{round(p.rect.height)}" for p in doc})
        return m
    # generic text recall
    if src.is_dir():
        src = src / "index.html"
    m["text_recall"] = recall(source_text(src, opts), text_of(main))
    if main.suffix == ".pdf":
        m["pages"] = fitz.open(main).page_count
    if jid.startswith(("05", "06", "07")):
        m["javascript_ran"] = "inserted by JavaScript" in text_of(main)
    if jid in ("18-pdf-to-word-roundtrip", "20-pdf-to-pptx-editable", "19-pdf-to-pptx-image", "23-pdf-to-excel-roundtrip",
               "17-pdf-to-word-realworld"):
        m["visual_similarity"] = visual_similarity(src, soffice_pdf(main))
    return m


def main():
    only = set(sys.argv[1:])
    if not only:
        shutil.rmtree(OUT, ignore_errors=True)
        shutil.rmtree(PREV, ignore_errors=True)
    OUT.mkdir(exist_ok=True)
    results = []
    for job in JOBS:
        jid, tool, raw_inputs, opts, out_name, kind, desc = job
        if only and jid not in only:
            continue
        inputs = [resolve(i) for i in raw_inputs]
        target = OUT / jid / out_name
        shutil.rmtree(OUT / jid, ignore_errors=True)
        (OUT / jid).mkdir(parents=True)
        t0 = time.time()
        try:
            produced = run_tool(get(tool), [str(p) for p in inputs], target, dict(opts))
            seconds = time.time() - t0
            produced = [Path(p) for p in produced]
            metrics = score(job, inputs, produced)
            preview(jid, f"{jid}: {desc}", inputs[0], produced[0])
            status = "ok"
        except Exception as e:  # noqa: BLE001 – keep going, report the failure
            seconds, produced, metrics, status = time.time() - t0, [], {}, f"FAILED: {e}"
        size = sum(p.stat().st_size for p in produced if p.exists())
        row = {"id": jid, "tool": tool, "description": desc, "inputs": [str(p.relative_to(HERE)) for p in inputs],
               "outputs": [str(p.relative_to(HERE)) for p in produced], "seconds": round(seconds, 2),
               "output_bytes": size, "status": status,
               "metrics": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in metrics.items()}}
        results.append(row)
        print(f"{jid:28} {status[:60]:10} {seconds:6.1f}s  {row['metrics']}")
    if not only:
        (HERE / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False))
        write_results_md(results)


def write_results_md(results):
    lines = ["# Automatic results", "", "Generated by `python examples/run_examples.py`. "
             "See [README.md](README.md) for the human evaluation.", "",
             "| Job | Tool | Time | Output | Metrics | Status |", "|---|---|--:|--:|---|---|"]
    for r in results:
        mets = ", ".join(f"{k.replace('_', ' ')}: {f'{v:.0%}' if isinstance(v, float) and v <= 1 else v}" for k, v in r["metrics"].items())
        size = f"{r['output_bytes'] / 1024:.0f} KB"
        lines.append(f"| [{r['id']}](previews/{r['id']}.jpg) | `{r['tool']}` | {r['seconds']}s | {size} | {mets} | {r['status']} |")
    (HERE / "RESULTS.md").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
