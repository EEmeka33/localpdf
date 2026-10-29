"""Organize PDF: merge, split, remove / extract / reorder pages, scan to PDF."""
from __future__ import annotations

import io
import math
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import (
    IMAGE_EXTS, ToolError, ensure_dir, open_pdf, parse_groups, parse_pages,
    require, run, save_pdf, tempdir,
)

PDF_LIKE = (".pdf", ".xps", ".epub", ".cbz", ".fb2", ".mobi") + IMAGE_EXTS


@tool(
    name="merge", title="Merge PDF", category="Organize PDF",
    help="Combine PDFs (and images) in the order you want into one PDF.",
    accept=PDF_LIKE, multiple=True, min_inputs=2,
    opts=[
        Opt("bookmarks", "bool", True, "Add a bookmark for each merged file (keeps their own bookmarks nested)."),
        Opt("password", "str", None, "Password for encrypted inputs (same for all)."),
    ],
)
def merge(inputs, output: Path, bookmarks=True, password=None):
    out = fitz.open()
    toc = []
    for path in inputs:
        src = open_pdf(path, password)
        start = out.page_count
        out.insert_pdf(src)
        if bookmarks:
            toc.append([1, Path(path).stem, start + 1])
            toc += [[lvl + 1, title, page + start] for lvl, title, page, *_ in src.get_toc() if page > 0]
        src.close()
    if bookmarks:
        out.set_toc(toc)
    return save_pdf(out, output)


@tool(
    name="split", title="Split PDF", category="Organize PDF",
    help="Separate one page or a whole set for easy conversion into independent PDF files.",
    output="dir",
    opts=[
        Opt("mode", "choice", "pages", "How to split.", ["pages", "ranges", "every", "size", "bookmarks"]),
        Opt("ranges", "str", None, "For mode=ranges: groups separated by ';', e.g. '1-3;4-6;7-'."),
        Opt("every", "int", 1, "For mode=every: number of pages per file."),
        Opt("max_size", "float", 5.0, "For mode=size: maximum size of each part in MB."),
        Opt("merge_ranges", "bool", False, "For mode=ranges: put all the ranges in one single PDF."),
    ],
)
def split(inputs, output: Path, mode="pages", ranges=None, every=1, max_size=5.0, merge_ranges=False):
    doc = open_pdf(inputs[0])
    n = doc.page_count
    stem = Path(inputs[0]).stem
    if mode == "pages":
        groups = [[i] for i in range(n)]
    elif mode == "every":
        every = max(1, int(every))
        groups = [list(range(i, min(i + every, n))) for i in range(0, n, every)]
    elif mode == "ranges":
        if not ranges:
            raise ToolError("--ranges is required for mode=ranges, e.g. '1-3;4-6'")
        groups = parse_groups(ranges, n)
        if merge_ranges:
            groups = [[p for g in groups for p in g]]
    elif mode == "bookmarks":
        starts = sorted({page - 1 for lvl, _, page, *_ in doc.get_toc() if lvl == 1 and page > 0} | {0})
        if len(starts) == 1:
            raise ToolError("this PDF has no top-level bookmarks to split on")
        groups = [list(range(a, b)) for a, b in zip(starts, starts[1:] + [n])]
    elif mode == "size":
        return _split_by_size(doc, output, stem, max_size)
    else:
        raise ToolError(f"unknown mode {mode}")

    ensure_dir(output)
    written = []
    width = len(str(len(groups)))
    for i, pages in enumerate(groups, 1):
        part = fitz.open()
        for p in pages:
            part.insert_pdf(doc, from_page=p, to_page=p)
        label = f"{pages[0] + 1}" if len(pages) == 1 else f"{pages[0] + 1}-{pages[-1] + 1}"
        target = output / f"{stem}_{str(i).zfill(width)}_p{label}.pdf"
        written += save_pdf(part, target)
    return written


def _split_by_size(doc, output: Path, stem: str, max_mb: float):
    ensure_dir(output)
    limit = max_mb * 1024 * 1024
    written, start, idx = [], 0, 1
    while start < doc.page_count:
        end = start
        # grow the part page by page while it stays below the limit
        while end + 1 < doc.page_count:
            trial = fitz.open()
            trial.insert_pdf(doc, from_page=start, to_page=end + 1)
            if len(trial.tobytes(garbage=3, deflate=True)) > limit:
                break
            end += 1
        part = fitz.open()
        part.insert_pdf(doc, from_page=start, to_page=end)
        written += save_pdf(part, output / f"{stem}_part{idx}.pdf")
        start, idx = end + 1, idx + 1
    return written


@tool(
    name="remove-pages", title="Remove pages", category="Organize PDF",
    help="Select and remove the PDF pages you don't need.",
    aliases=("remove", "delete-pages"),
    opts=[Opt("pages", "str", None, "Pages to delete, e.g. '2,5-7,last'.", required=True)],
)
def remove_pages(inputs, output: Path, pages):
    doc = open_pdf(inputs[0])
    drop = set(parse_pages(pages, doc.page_count))
    if len(drop) == doc.page_count:
        raise ToolError("cannot remove every page")
    doc.select([i for i in range(doc.page_count) if i not in drop])
    return save_pdf(doc, output)


@tool(
    name="extract-pages", title="Extract pages", category="Organize PDF",
    help="Get a new document containing only the desired pages.",
    aliases=("extract",),
    opts=[
        Opt("pages", "str", None, "Pages to keep, e.g. '1,3-5,odd'.", required=True),
        Opt("separate", "bool", False, "Write each extracted page to its own PDF (output becomes a folder)."),
    ],
)
def extract_pages(inputs, output: Path, pages, separate=False):
    doc = open_pdf(inputs[0])
    keep = parse_pages(pages, doc.page_count)
    if separate:
        ensure_dir(output)
        written = []
        for p in keep:
            part = fitz.open()
            part.insert_pdf(doc, from_page=p, to_page=p)
            written += save_pdf(part, output / f"{Path(inputs[0]).stem}_p{p + 1}.pdf")
        return written
    doc.select(keep)
    return save_pdf(doc, output)


@tool(
    name="organize", title="Organize PDF", category="Organize PDF",
    help="Sort, duplicate, delete, rotate or insert blank pages – all in one order string.",
    aliases=("reorder",),
    opts=[
        Opt("order", "str", None,
            "New page order. Tokens: '3', '1-2', 'last', 'blank' (blank page), "
            "suffix ':90'/':180'/':270' to rotate, e.g. '3,1-2,blank,4:90,5-'.", required=True),
    ],
)
def organize(inputs, output: Path, order):
    src = open_pdf(inputs[0])
    out = fitz.open()
    for raw in [t.strip() for t in order.split(",") if t.strip()]:
        tok, _, rot = raw.partition(":")
        if tok.lower() == "blank":
            ref = out[-1].rect if out.page_count else src[0].rect
            out.new_page(width=ref.width, height=ref.height)
            continue
        for p in parse_pages(tok, src.page_count):
            out.insert_pdf(src, from_page=p, to_page=p)
            if rot:
                pg = out[-1]
                pg.set_rotation((pg.rotation + int(rot)) % 360)
    return save_pdf(out, output)


@tool(
    name="scan-to-pdf", title="Scan to PDF", category="Organize PDF",
    help="Turn photos of documents – or pages from a SANE scanner – into a clean, optionally searchable PDF.",
    aliases=("scan",), accept=IMAGE_EXTS, multiple=True, min_inputs=0,
    requires=("scanimage", "tesseract"),
    opts=[
        Opt("scanner", "bool", False, "Acquire pages from a connected scanner with `scanimage` (SANE)."),
        Opt("device", "str", None, "SANE device name (see `scanimage -L`)."),
        Opt("pages_to_scan", "int", 1, "Number of pages to scan (scanner mode, flatbed)."),
        Opt("resolution", "int", 300, "Scanner resolution in DPI."),
        Opt("mode", "choice", "auto", "Image clean-up.", ["auto", "color", "grayscale", "bw", "none"]),
        Opt("ocr", "bool", False, "Make the result searchable with OCR (needs tesseract)."),
        Opt("lang", "str", "eng", "OCR language(s), e.g. 'eng+fra'."),
    ],
)
def scan_to_pdf(inputs, output: Path, scanner=False, device=None, pages_to_scan=1, resolution=300,
                mode="auto", ocr=False, lang="eng"):
    from PIL import Image, ImageFilter, ImageOps

    with tempdir() as tmp:
        images = [Path(i) for i in inputs]
        if scanner:
            exe = require("scanimage", hint="Install SANE (apt install sane-utils).")
            for i in range(int(pages_to_scan)):
                target = tmp / f"scan_{i:03d}.png"
                cmd = [exe, "--format=png", f"--resolution={resolution}", "-o", target]
                if device:
                    cmd += ["-d", device]
                run(cmd, timeout=300)
                images.append(target)
        if not images:
            raise ToolError("give image files or use --scanner")

        doc = fitz.open()
        for img_path in images:
            img = ImageOps.exif_transpose(Image.open(img_path))
            img = _clean_scan(img, mode, ImageOps, ImageFilter)
            buf = io.BytesIO()
            if img.mode == "1":
                img.save(buf, "PNG", optimize=True)
            else:
                img.convert("RGB" if img.mode not in ("L", "RGB") else img.mode).save(buf, "JPEG", quality=85, optimize=True)
            dpi = resolution if scanner else 200
            w, h = img.width * 72 / dpi, img.height * 72 / dpi
            page = doc.new_page(width=w, height=h)
            page.insert_image(page.rect, stream=buf.getvalue())
        result = save_pdf(doc, output)
    if ocr:
        from .optimize import ocr as run_ocr

        tmp_out = output.with_suffix(".ocr.pdf")
        run_ocr([str(output)], tmp_out, lang=lang)
        tmp_out.replace(output)
    return result


def _clean_scan(img, mode, ImageOps, ImageFilter):
    if mode == "none":
        return img
    if mode == "auto":
        # documents photographed with a phone: grayscale unless clearly colourful
        small = img.convert("RGB").resize((64, 64))
        sat = sum(max(px) - min(px) for px in small.getdata()) / (64 * 64)
        mode = "color" if sat > 40 else "grayscale"
    if mode == "color":
        return ImageOps.autocontrast(img.convert("RGB"), cutoff=1)
    gray = ImageOps.autocontrast(img.convert("L"), cutoff=2)
    if mode == "grayscale":
        return gray
    # black & white: subtract the (blurred) illumination so shadows don't turn black
    from PIL import ImageChops

    background = gray.filter(ImageFilter.GaussianBlur(radius=max(gray.size) // 40 or 1))
    ink = ImageChops.subtract(background, gray)  # how much darker than its surroundings
    return ink.point(lambda v: 0 if v > 25 else 255).convert("1")


# ------------------------------------------------------------------ extras
@tool(
    name="reverse", title="Reverse pages", category="Extras", extra=True,
    help="Reverse the page order (handy for scans done back-to-front).",
)
def reverse(inputs, output: Path):
    doc = open_pdf(inputs[0])
    doc.select(list(range(doc.page_count - 1, -1, -1)))
    return save_pdf(doc, output)


@tool(
    name="interleave", title="Alternate & mix", category="Extras", extra=True,
    help="Mix pages of several PDFs alternately (A1,B1,A2,B2…) – perfect for single-sided duplex scans.",
    multiple=True, min_inputs=2,
    opts=[Opt("reverse_second", "bool", False, "Reverse the second file first (back sides scanned in reverse).")],
)
def interleave(inputs, output: Path, reverse_second=False):
    docs = [open_pdf(p) for p in inputs]
    out = fitz.open()
    orders = [list(range(d.page_count)) for d in docs]
    if reverse_second and len(orders) > 1:
        orders[1].reverse()
    for i in range(max(len(o) for o in orders)):
        for d, o in zip(docs, orders):
            if i < len(o):
                out.insert_pdf(d, from_page=o[i], to_page=o[i])
    return save_pdf(out, output)


@tool(
    name="n-up", title="Pages per sheet (N-up)", category="Extras", extra=True,
    help="Print several pages on a single sheet (2, 4, 6, 8, 9 or 16 per sheet).", aliases=("nup",),
    opts=[
        Opt("per_sheet", "choice", "2", "Pages per sheet.", ["2", "4", "6", "8", "9", "16"]),
        Opt("paper", "choice", "a4", "Output sheet size.", ["a4", "a3", "letter", "legal"]),
        Opt("margin", "float", 10, "Gap around each page in points."),
    ],
)
def n_up(inputs, output: Path, per_sheet="2", paper="a4", margin=10):
    from ..utils import PAPER_SIZES

    src = open_pdf(inputs[0])
    n = int(per_sheet)
    cols, rows = {2: (2, 1), 4: (2, 2), 6: (3, 2), 8: (4, 2), 9: (3, 3), 16: (4, 4)}[n]
    w, h = PAPER_SIZES[paper]
    if cols > rows:  # landscape sheet for wide grids
        w, h = h, w
    out = fitz.open()
    cw, ch = w / cols, h / rows
    for i in range(src.page_count):
        if i % n == 0:
            sheet = out.new_page(width=w, height=h)
        k = i % n
        r, c = divmod(k, cols)
        cell = fitz.Rect(c * cw + margin, r * ch + margin, (c + 1) * cw - margin, (r + 1) * ch - margin)
        sheet.show_pdf_page(cell, src, i)
    return save_pdf(out, output)


@tool(
    name="resize", title="Resize pages", category="Extras", extra=True,
    help="Scale every page to a standard paper size (A4, Letter…) keeping proportions.",
    opts=[
        Opt("paper", "choice", "a4", "Target size.", ["a3", "a4", "a5", "letter", "legal", "tabloid"]),
        Opt("orientation", "choice", "auto", "Page orientation.", ["auto", "portrait", "landscape"]),
    ],
)
def resize(inputs, output: Path, paper="a4", orientation="auto"):
    from ..utils import PAPER_SIZES

    src = open_pdf(inputs[0])
    out = fitz.open()
    pw, ph = PAPER_SIZES[paper]
    for i, page in enumerate(src):
        landscape = page.rect.width > page.rect.height if orientation == "auto" else orientation == "landscape"
        w, h = (ph, pw) if landscape else (pw, ph)
        new = out.new_page(width=w, height=h)
        new.show_pdf_page(new.rect, src, i, keep_proportion=True)
    return save_pdf(out, output)


@tool(
    name="remove-blank", title="Remove blank pages", category="Extras", extra=True,
    help="Detect and drop empty pages (scanner separators, accidental blanks).",
    opts=[Opt("threshold", "float", 0.995, "Fraction of near-white pixels above which a page counts as blank.")],
)
def remove_blank(inputs, output: Path, threshold=0.995):
    doc = open_pdf(inputs[0])
    keep = []
    for i, page in enumerate(doc):
        if page.get_text().strip():
            keep.append(i)
            continue
        pix = page.get_pixmap(dpi=40, colorspace=fitz.csGRAY)
        samples = pix.samples
        white = sum(1 for b in samples if b > 235) / max(1, len(samples))
        if white < threshold:
            keep.append(i)
    if not keep:
        raise ToolError("every page looks blank – nothing would be left")
    doc.select(keep)
    return save_pdf(doc, output)


@tool(
    name="insert-blank", title="Insert blank pages", category="Extras", extra=True,
    help="Insert blank pages after given pages (or after every page for note-taking).",
    opts=[Opt("after", "str", "all", "Pages after which to insert a blank page, e.g. '1,3' or 'all'.")],
)
def insert_blank(inputs, output: Path, after="all"):
    src = open_pdf(inputs[0])
    targets = set(parse_pages(after, src.page_count))
    out = fitz.open()
    for i in range(src.page_count):
        out.insert_pdf(src, from_page=i, to_page=i)
        if i in targets:
            r = src[i].rect
            out.new_page(width=r.width, height=r.height)
    return save_pdf(out, output)


@tool(
    name="booklet", title="Booklet imposition", category="Extras", extra=True,
    help="Arrange pages 2-up for saddle-stitch booklet printing (fold & staple).",
)
def booklet(inputs, output: Path):
    src = open_pdf(inputs[0])
    n = src.page_count
    total = int(math.ceil(n / 4.0) * 4)
    w, h = src[0].rect.width, src[0].rect.height
    out = fitz.open()
    order = []
    lo, hi = 0, total - 1
    while lo < hi:
        order += [(hi, lo), (lo + 1, hi - 1)]
        lo, hi = lo + 2, hi - 2
    for left, right in order:
        sheet = out.new_page(width=w * 2, height=h)
        for idx, x in ((left, 0), (right, w)):
            if idx < n:
                sheet.show_pdf_page(fitz.Rect(x, 0, x + w, h), src, idx)
    return save_pdf(out, output)
