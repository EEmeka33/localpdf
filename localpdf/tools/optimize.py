"""Optimize PDF: compress, repair, OCR."""
from __future__ import annotations

import shutil
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import ToolError, ghostscript, human_size, open_pdf, run, save_pdf, tempdir, which

GS_PRESETS = {"low": "/printer", "recommended": "/ebook", "extreme": "/screen"}
IMG_DPI = {"low": (300, 200), "recommended": (200, 150), "extreme": (120, 72)}  # threshold, target
IMG_QUALITY = {"low": 85, "recommended": 70, "extreme": 45}


@tool(
    name="compress", title="Compress PDF", category="Optimize PDF",
    help="Reduce file size while optimizing for maximal PDF quality.",
    requires=("gs",),
    opts=[
        Opt("level", "choice", "recommended", "Compression level.", ["low", "recommended", "extreme"]),
        Opt("grayscale", "bool", False, "Also convert to grayscale (saves more)."),
        Opt("engine", "choice", "auto", "Compressor to use.", ["auto", "ghostscript", "pymupdf"]),
    ],
)
def compress(inputs, output: Path, level="recommended", grayscale=False, engine="auto"):
    src = Path(inputs[0])
    original = src.stat().st_size
    candidates = []
    with tempdir() as tmp:
        if engine in ("auto", "ghostscript") and ghostscript():
            gs_out = tmp / "gs.pdf"
            cmd = [ghostscript(), "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.6", "-dNOPAUSE", "-dBATCH",
                   "-dQUIET", "-dSAFER", f"-dPDFSETTINGS={GS_PRESETS[level]}", "-dDetectDuplicateImages=true",
                   "-dCompressFonts=true", "-dSubsetFonts=true"]
            if grayscale:
                cmd += ["-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"]
            run(cmd + [f"-sOutputFile={gs_out}", src])
            candidates.append(gs_out)
        elif engine == "ghostscript":
            raise ToolError("Ghostscript is not installed (apt install ghostscript)")

        if engine in ("auto", "pymupdf"):
            mu_out = tmp / "mu.pdf"
            doc = open_pdf(src)
            threshold, target = IMG_DPI[level]
            try:
                doc.rewrite_images(dpi_threshold=threshold, dpi_target=target, quality=IMG_QUALITY[level],
                                   lossy=True, lossless=True, bitonal=True, color=True, gray=True,
                                   set_to_gray=grayscale)
            except (AttributeError, TypeError):  # older PyMuPDF
                pass
            if hasattr(doc, "subset_fonts"):
                doc.subset_fonts()
            doc.scrub(metadata=False, xml_metadata=False, thumbnails=True, reset_fields=False,
                      reset_responses=False, javascript=False, attached_files=False, clean_pages=True,
                      embedded_files=False, hidden_text=False, redactions=False, remove_links=False)
            doc.save(mu_out, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1, clean=True)
            doc.close()
            candidates.append(mu_out)

        best = min(candidates, key=lambda p: p.stat().st_size)
        if best.stat().st_size >= original:
            shutil.copyfile(src, output)  # already optimal – never make it bigger
        else:
            shutil.copyfile(best, output)
    new = output.stat().st_size
    saved = 100 * (1 - new / original) if original else 0
    print(f"{src.name}: {human_size(original)} → {human_size(new)} ({saved:.0f}% smaller)")
    return [output]


@tool(
    name="repair", title="Repair PDF", category="Optimize PDF",
    help="Repair a damaged PDF and recover data from corrupt PDF.",
    requires=("qpdf", "gs"),
    opts=[Opt("password", "str", None, "Password, if the damaged file is encrypted.")],
)
def repair(inputs, output: Path, password=None):
    src = Path(inputs[0])
    errors = []
    # 1) qpdf (via pikepdf) reconstructs broken xref tables and streams
    try:
        import pikepdf

        with pikepdf.open(src, password=password or "", attempt_recovery=True) as pdf:
            pdf.save(output, fix_metadata_version=True, object_stream_mode=pikepdf.ObjectStreamMode.generate)
        fitz.open(output).close()
        return [output]
    except Exception as e:  # noqa: BLE001 – try the next strategy
        errors.append(f"pikepdf: {e}")
    # 2) MuPDF's own repair on load
    try:
        doc = fitz.open(src)
        if doc.needs_pass:
            doc.authenticate(password or "")
        if doc.page_count == 0:
            raise RuntimeError("no pages recovered")
        return save_pdf(doc, output, garbage=4, clean=True)
    except Exception as e:  # noqa: BLE001
        errors.append(f"mupdf: {e}")
    # 3) Ghostscript re-interprets the whole file
    if ghostscript():
        try:
            run([ghostscript(), "-o", output, "-sDEVICE=pdfwrite", "-dPDFSTOPONERROR=false", src])
            return [output]
        except ToolError as e:
            errors.append(str(e))
    raise ToolError("could not repair the file: " + "; ".join(errors))


@tool(
    name="ocr", title="OCR PDF", category="Optimize PDF",
    help="Convert scanned PDFs into searchable and selectable documents.",
    accept=(".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"),
    requires=("tesseract", "ocrmypdf"),
    opts=[
        Opt("lang", "str", "eng", "Tesseract language(s), e.g. 'eng', 'fra', 'eng+deu'."),
        Opt("force", "bool", False, "OCR even pages that already contain text (rasterizes them)."),
        Opt("deskew", "bool", True, "Straighten crooked scans (ocrmypdf only)."),
        Opt("dpi", "int", 300, "Rendering resolution for the built-in OCR engine."),
    ],
)
def ocr(inputs, output: Path, lang="eng", force=False, deskew=True, dpi=300):
    src = Path(inputs[0])
    ocrmypdf = which("ocrmypdf")
    if ocrmypdf and src.suffix.lower() == ".pdf":
        cmd = [ocrmypdf, "-l", lang, "--output-type", "pdf", "--optimize", "1"]
        cmd += ["--force-ocr"] if force else ["--skip-text"]
        if deskew and force:
            cmd.append("--deskew")
        run(cmd + [src, output], timeout=3600)
        return [output]

    if not which("tesseract"):
        raise ToolError("OCR needs Tesseract: apt install tesseract-ocr (and tesseract-ocr-<lang> for other languages)")
    tessdata = _tessdata()
    doc = open_pdf(src)
    for i in range(doc.page_count):
        page = doc[i]
        if page.get_text().strip() and not force:
            continue  # already searchable
        if force:  # replace the page by a fresh rendering + OCR text (like ocrmypdf --force-ocr)
            pix = page.get_pixmap(dpi=dpi)  # must be RGB: MuPDF's OCR writer silently drops text for gray pixmaps
            ocr_pdf = fitz.open("pdf", pix.pdfocr_tobytes(language=lang, tessdata=tessdata))
            rect = page.rect
            doc.delete_page(i)
            new = doc.new_page(pno=i, width=rect.width, height=rect.height)
            new.show_pdf_page(new.rect, ocr_pdf, 0)
            continue
        _add_text_layer(page, lang, dpi, tessdata)
    return save_pdf(doc, output, garbage=4)


def _add_text_layer(page, lang, dpi, tessdata):
    """Lay invisible OCR words over the untouched page: original image quality and file size are kept."""
    tp = page.get_textpage_ocr(language=lang, dpi=dpi, full=True, tessdata=tessdata)
    font = fitz.Font("helv")
    for x0, y0, x1, y1, word, *_ in page.get_text("words", textpage=tp):
        height = y1 - y0
        size = height * 0.9
        natural = font.text_length(word, fontsize=size)
        if natural <= 0:
            continue
        # stretch each word to its box so text selection lines up with the picture
        origin = fitz.Point(x0, y1 - height * 0.2)
        tw = fitz.TextWriter(page.rect)
        tw.append(origin, word, font=font, fontsize=size)
        tw.write_text(page, render_mode=3, morph=(origin, fitz.Matrix((x1 - x0) / natural, 1)))


def _tessdata() -> str | None:
    import glob
    import os

    if os.environ.get("TESSDATA_PREFIX"):
        return os.environ["TESSDATA_PREFIX"]
    for pattern in ("/usr/share/tesseract-ocr/*/tessdata", "/usr/share/tessdata", "/usr/share/tesseract/tessdata",
                    "/usr/local/share/tessdata"):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[-1]
    return None
