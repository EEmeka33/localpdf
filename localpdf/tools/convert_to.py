"""Convert to PDF: images, Office documents, HTML (files, folders & URLs), Markdown, text."""
from __future__ import annotations

import html as html_lib
import io
import os
import re
import shutil
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import (
    EXCEL_EXTS, HTML_EXTS, IMAGE_EXTS, PAPER_SIZES, PPT_EXTS, WORD_EXTS, ToolError, ensure_dir,
    save_pdf, soffice_convert, tempdir, which,
)

MARGINS = {"none": 0, "small": 20, "big": 50}


# ------------------------------------------------------------------ images
@tool(
    name="jpg-to-pdf", title="JPG to PDF", category="Convert to PDF",
    help="Convert JPG, PNG, WEBP, TIFF, HEIC… images to PDF. Adjust orientation and margins.",
    aliases=("img-to-pdf", "image-to-pdf", "png-to-pdf"), accept=IMAGE_EXTS, multiple=True,
    opts=[
        Opt("page_size", "choice", "fit", "Page size ('fit' = same as image).", ["fit", "a4", "letter", "legal", "a3", "a5"]),
        Opt("orientation", "choice", "auto", "Page orientation.", ["auto", "portrait", "landscape"]),
        Opt("margin", "choice", "none", "Margin around the image.", ["none", "small", "big"]),
        Opt("separate", "bool", False, "One PDF per image instead of merging them (output becomes a folder)."),
        Opt("quality", "int", 90, "JPEG quality used when an image has to be re-encoded."),
    ],
)
def jpg_to_pdf(inputs, output: Path, page_size="fit", orientation="auto", margin="none", separate=False, quality=90):
    from PIL import Image, ImageOps

    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()
    except ImportError:
        pass

    def add(doc, path):
        img = Image.open(path)
        frames = getattr(img, "n_frames", 1)
        for f in range(frames):  # multi-page TIFF / animated GIF
            img.seek(f)
            frame = ImageOps.exif_transpose(img.copy())
            if frame.mode in ("P", "LA", "RGBA", "CMYK", "I;16", "I", "F"):
                frame = frame.convert("RGBA" if "A" in frame.mode or "transparency" in frame.info else "RGB")
            buf = io.BytesIO()
            fmt = "PNG" if frame.mode == "RGBA" or path.suffix.lower() == ".png" else "JPEG"
            if fmt == "JPEG" and path.suffix.lower() in (".jpg", ".jpeg") and frames == 1 and not _rotated(path):
                buf.write(Path(path).read_bytes())  # keep original JPEG bytes – no quality loss
            else:
                frame.save(buf, fmt, quality=quality)
            iw, ih = frame.size
            dpi = float(frame.info.get("dpi", (96, 96))[0] or 96)
            if not 30 <= dpi <= 2400:  # missing or bogus resolution (e.g. 1×1 in some faxes/scans)
                dpi = 150 if min(iw, ih) > 1200 else 96
            if page_size == "fit":
                m = MARGINS[margin]
                w, h = iw * 72 / dpi + 2 * m, ih * 72 / dpi + 2 * m
            else:
                w, h = PAPER_SIZES[page_size]
                landscape = iw > ih if orientation == "auto" else orientation == "landscape"
                if landscape:
                    w, h = h, w
            page = doc.new_page(width=w, height=h)
            m = MARGINS[margin]
            page.insert_image(fitz.Rect(m, m, w - m, h - m), stream=buf.getvalue(), keep_proportion=True)

    paths = [Path(i) for i in inputs]
    if separate:
        ensure_dir(output)
        written = []
        for p in paths:
            doc = fitz.open()
            add(doc, p)
            written += save_pdf(doc, output / f"{p.stem}.pdf")
        return written
    doc = fitz.open()
    for p in paths:
        add(doc, p)
    return save_pdf(doc, output)


def _rotated(path: Path) -> bool:
    from PIL import Image

    try:
        return Image.open(path).getexif().get(0x0112, 1) not in (1, None)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------ office
def _office(inputs, output: Path):
    src = Path(inputs[0])
    with tempdir() as tmp:
        produced = soffice_convert(src, tmp, "pdf")
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(produced, output)
    return [output]


for _name, _title, _exts, _aliases, _desc in [
    ("word-to-pdf", "Word to PDF", WORD_EXTS, ("docx-to-pdf", "doc-to-pdf"),
     "Make DOC, DOCX, ODT and RTF files easy to read by converting them to PDF."),
    ("powerpoint-to-pdf", "PowerPoint to PDF", PPT_EXTS, ("ppt-to-pdf", "pptx-to-pdf"),
     "Make PPT, PPTX and ODP slideshows easy to view by converting them to PDF."),
]:
    tool(name=_name, title=_title, category="Convert to PDF", help=_desc, accept=_exts, aliases=_aliases,
         requires=("soffice",))(_office)

@tool(
    name="excel-to-pdf", title="Excel to PDF", category="Convert to PDF",
    help="Make EXCEL / ODS / CSV spreadsheets easy to read by converting them to PDF.",
    accept=EXCEL_EXTS, aliases=("xls-to-pdf", "xlsx-to-pdf"), requires=("soffice",),
    opts=[Opt("fit", "choice", "width", "'width' = all columns on the page width (no columns cut onto extra pages), "
              "'page' = every sheet on a single page, 'none' = the file's own print settings.", ["width", "page", "none"])],
)
def excel_to_pdf(inputs, output: Path, fit="width"):
    src = Path(inputs[0])
    with tempdir() as tmp:
        fmt = "pdf"
        if fit == "page":
            fmt = 'pdf:calc_pdf_Export:{"SinglePageSheets":{"type":"boolean","value":"true"}}'
        elif fit == "width" and src.suffix.lower() in (".xlsx", ".xlsm"):
            src = _fit_columns_to_width(src, tmp / src.name)
        produced = soffice_convert(src, tmp, fmt)
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(produced, output)
    return [output]


def _fit_columns_to_width(src: Path, dst: Path) -> Path:
    """Copy of an .xlsx whose sheets print 1 page wide (any number of pages tall).

    Edits the sheet XML directly (instead of round-tripping through openpyxl, which drops charts and images).
    """
    import zipfile

    def patch(xml: str) -> str:
        # <sheetPr><pageSetUpPr fitToPage="1"/></sheetPr> must be the first child of <worksheet>
        if re.search(r"<pageSetUpPr\b", xml):
            def fit_flag(m):
                attrs = re.sub(r' fitToPage="[^"]*"', "", m.group(1))
                return f'<pageSetUpPr{attrs} fitToPage="1"{m.group(2)}>'
            xml = re.sub(r"<pageSetUpPr\b([^>]*?)(/?)>", fit_flag, xml, count=1)
        elif re.search(r"<sheetPr\b[^>]*/>", xml):
            xml = re.sub(r"<sheetPr\b([^>]*)/>", r'<sheetPr\1><pageSetUpPr fitToPage="1"/></sheetPr>', xml, count=1)
        elif "<sheetPr" in xml:
            xml = re.sub(r"(<sheetPr\b[^>]*>)", r'\1<pageSetUpPr fitToPage="1"/>', xml, count=1)
        else:
            xml = re.sub(r"(<worksheet\b[^>]*>)", r'\1<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>', xml, count=1)
        if "<pageSetup" in xml:
            def fix(m):
                attrs = re.sub(r' fitTo(Width|Height)="[^"]*"', "", m.group(1))
                return f'<pageSetup{attrs} fitToWidth="1" fitToHeight="0"{m.group(2)}>'
            xml = re.sub(r"<pageSetup\b([^>]*?)(/?)>", fix, xml, count=1)
        else:
            anchor = re.search(r"<pageMargins\b[^>]*/>|</pageMargins>", xml)
            tag = '<pageSetup fitToWidth="1" fitToHeight="0"/>'
            if anchor:
                xml = xml[:anchor.end()] + tag + xml[anchor.end():]
            else:
                nxt = re.search(r"<(headerFooter|rowBreaks|colBreaks|drawing|legacyDrawing|picture|tableParts|extLst)\b|</worksheet>", xml)
                xml = xml[:nxt.start()] + tag + xml[nxt.start():]
        return xml

    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", item.filename):
                data = patch(data.decode("utf-8")).encode("utf-8")
            zout.writestr(item, data)
    return dst


tool(
    name="office-to-pdf", title="Any document to PDF", category="Extras", extra=True,
    help="Convert anything LibreOffice can open (ODG, VSD, PUB, EPUB, WPS, XML, …) to PDF.",
    accept=(), requires=("soffice",),
)(_office)


# ------------------------------------------------------------------ HTML
HTML_OPTS = [
    Opt("paper", "choice", "a4", "Page size ('single' = one long page, like a screenshot).",
        ["a4", "letter", "legal", "a3", "a5", "tabloid", "single"]),
    Opt("orientation", "choice", "portrait", "Page orientation.", ["portrait", "landscape"]),
    Opt("margin", "choice", "small", "Page margins.", ["none", "small", "big"]),
    Opt("media", "choice", "print", "CSS media to emulate ('screen' looks like the browser).", ["print", "screen"]),
    Opt("width", "int", 1280, "Viewport width in pixels (affects responsive layouts)."),
    Opt("wait", "int", 500, "Extra milliseconds to wait for JavaScript / lazy content."),
    Opt("background", "bool", True, "Print background colours and images."),
    Opt("block_ads", "bool", False, "Try to block common ad / tracker domains when loading URLs."),
    Opt("engine", "choice", "auto", "Rendering engine.", ["auto", "chromium", "weasyprint", "wkhtmltopdf", "libreoffice", "mupdf"]),
]


@tool(
    name="html-to-pdf", title="HTML to PDF", category="Convert to PDF",
    help="Convert webpages to PDF – from a URL, a local .html file, a folder with index.html or a .zip of a site (CSS, images and JS included).",
    accept=HTML_EXTS + (".zip",), allow_url=True, requires=("chromium", "weasyprint"), opts=HTML_OPTS,
)
def html_to_pdf(inputs, output: Path, paper="a4", orientation="portrait", margin="small", media="print", width=1280,
                wait=500, background=True, block_ads=False, engine="auto"):
    source = inputs[0]
    with tempdir() as tmp:
        url = _resolve_html_source(source, tmp)
        engines = [engine] if engine != "auto" else ["chromium", "weasyprint", "wkhtmltopdf", "libreoffice", "mupdf"]
        errors = []
        for eng in engines:
            try:
                fn = ENGINES[eng]
            except KeyError:
                raise ToolError(f"unknown engine {eng}") from None
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                fn(url, output, paper=paper, orientation=orientation, margin=MARGINS[margin], media=media,
                   width=width, wait=wait, background=background, block_ads=block_ads)
                if engine == "auto" and eng != "chromium":
                    print(f"(rendered with {eng})")
                return [output]
            except _Unavailable as e:
                errors.append(f"{eng}: {e}")
        raise ToolError("no HTML engine available. Install one: `pip install playwright && playwright install chromium` "
                        "(best), or `pip install weasyprint`. Details: " + "; ".join(errors))


class _Unavailable(Exception):
    pass


def _resolve_html_source(source: str, tmp: Path) -> str:
    if re.match(r"^[a-z]+://", source, re.I):
        return source
    if re.match(r"^(www\.|[\w-]+\.[a-z]{2,}(/|$))", source, re.I) and not Path(source).exists():
        return "https://" + source
    p = Path(source).resolve()
    if not p.exists():
        raise ToolError(f"file not found: {source}")
    if p.suffix.lower() == ".zip":  # zipped website export
        import zipfile

        with zipfile.ZipFile(p) as z:
            for member in z.namelist():  # refuse path traversal
                if member.startswith("/") or ".." in Path(member).parts:
                    raise ToolError(f"unsafe path in zip: {member}")
            z.extractall(tmp / "site")
        p = tmp / "site"
    if p.is_dir():
        for name in ("index.html", "index.htm"):
            if (p / name).exists():
                return (p / name).as_uri()
        pages = sorted(p.rglob("*.htm*"))
        if not pages:
            raise ToolError(f"no .html file found in {source}")
        return pages[0].as_uri()
    return p.as_uri()


AD_HOSTS = ("doubleclick.net", "googlesyndication.com", "google-analytics.com", "googletagmanager.com",
            "adservice.google", "facebook.net", "scorecardresearch.com", "taboola.com", "outbrain.com", "criteo")


def _chromium(url, output, paper, orientation, margin, media, width, wait, background, block_ads):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise _Unavailable("playwright not installed") from e
    try:
        with sync_playwright() as pw:
            exe = os.environ.get("LOCALPDF_CHROMIUM")
            try:
                browser = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
            except Exception:  # noqa: BLE001 – fall back to a system Chrome/Chromium
                exe = which("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "brave-browser")
                if not exe:
                    raise
                browser = pw.chromium.launch(executable_path=exe)
            page = browser.new_page(viewport={"width": width, "height": 900})
            if block_ads:
                page.route("**/*", lambda route: route.abort() if any(h in route.request.url for h in AD_HOSTS)
                           else route.continue_())
            page.goto(url, wait_until="networkidle", timeout=90_000)
            page.emulate_media(media=media)
            # trigger lazy-loaded images
            page.evaluate("""async () => { for (let y = 0; y < document.body.scrollHeight; y += 800) {
                window.scrollTo(0, y); await new Promise(r => setTimeout(r, 50)); } window.scrollTo(0, 0); }""")
            page.wait_for_timeout(wait)
            m = f"{margin / 72:.3f}in"  # Playwright accepts px/in/cm/mm, not pt
            opts = dict(path=str(output), print_background=background, margin=dict(top=m, bottom=m, left=m, right=m))
            if paper == "single":
                height = page.evaluate("document.documentElement.scrollHeight")
                # margins are in pt, the page in CSS px; widen the page by the margins so the content
                # area is exactly the viewport we measured (otherwise text re-wraps and grows taller)
                margin_px = margin * 96 / 72
                opts.update(width=f"{width + 2 * margin_px:.0f}px", height=f"{height + 2 * margin_px + 2:.0f}px")
            else:
                opts.update(format=paper.capitalize() if paper != "a4" else "A4", landscape=orientation == "landscape")
                if paper in ("a3", "a5"):
                    opts["format"] = paper.upper()
            page.pdf(**opts)
            browser.close()
    except _Unavailable:
        raise
    except Exception as e:  # noqa: BLE001
        if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
            raise _Unavailable("Chromium not downloaded – run `playwright install chromium`") from e
        raise ToolError(f"Chromium failed to render the page: {e}") from e


def _page_css(paper, orientation, margin):
    size = "auto" if paper == "single" else f"{paper} {orientation}"
    return f"@page {{ size: {size}; margin: {margin}pt; }}"


def _weasyprint(url, output, paper, orientation, margin, media, background, **_):
    try:
        from weasyprint import CSS, HTML
    except (ImportError, OSError) as e:
        raise _Unavailable("weasyprint not installed") from e
    HTML(url=url, media_type=media).write_pdf(str(output), stylesheets=[CSS(string=_page_css(paper, orientation, margin))])


def _wkhtmltopdf(url, output, paper, orientation, margin, media, wait, background, **_):
    from ..utils import run

    exe = which("wkhtmltopdf")
    if not exe:
        raise _Unavailable("wkhtmltopdf not installed")
    cmd = [exe, "--enable-local-file-access", "--javascript-delay", str(wait), "-O", orientation.capitalize(),
           "-T", f"{margin}pt", "-B", f"{margin}pt", "-L", f"{margin}pt", "-R", f"{margin}pt"]
    if paper != "single":
        cmd += ["-s", paper.upper() if paper.startswith("a") else paper.capitalize()]
    cmd += ["--print-media-type"] if media == "print" else []
    cmd += [] if background else ["--no-background"]
    run(cmd + [url, output])


def _libreoffice(url, output, **_):
    if not url.startswith("file://") or not which("soffice", "libreoffice"):
        raise _Unavailable("LibreOffice only converts local files")
    from urllib.parse import unquote, urlparse

    src = Path(unquote(urlparse(url).path))
    with tempdir() as tmp:
        produced = soffice_convert(src, tmp, "pdf:writer_web_pdf_Export")
        shutil.move(produced, output)


def _mupdf(url, output, paper, orientation, margin, **_):
    """Last resort: MuPDF's built-in (basic) HTML layout engine – no JavaScript, limited CSS."""
    if not url.startswith("file://"):
        raise _Unavailable("the built-in engine only renders local files")
    from urllib.parse import unquote, urlparse

    src = Path(unquote(urlparse(url).path))
    _story_to_pdf(src.read_text(encoding="utf-8", errors="replace"), output, paper, orientation, margin,
                  archive=str(src.parent))


ENGINES = {"chromium": _chromium, "weasyprint": _weasyprint, "wkhtmltopdf": _wkhtmltopdf,
           "libreoffice": _libreoffice, "mupdf": _mupdf}


def _story_to_pdf(html: str, output: Path, paper="a4", orientation="portrait", margin=36, archive=None, css=""):
    w, h = PAPER_SIZES.get(paper, PAPER_SIZES["a4"])
    if orientation == "landscape":
        w, h = h, w
    mediabox = fitz.Rect(0, 0, w, h)
    where = mediabox + (margin, margin, -margin, -margin)
    story = fitz.Story(html=html, user_css=css or None, archive=archive)
    writer = fitz.DocumentWriter(str(output))
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


# ------------------------------------------------------------------ Markdown & text (extras)
MD_CSS = """
body { font-family: -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.55;
       color: #1f2328; max-width: 900px; margin: 0 auto; }
h1, h2 { border-bottom: 1px solid #d8dee4; padding-bottom: .3em; }
code { background: #f3f4f6; padding: .1em .35em; border-radius: 4px; font-family: 'DejaVu Sans Mono', monospace; font-size: 90%; }
pre { background: #f6f8fa; padding: 12px 14px; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
blockquote { border-left: 4px solid #d0d7de; margin: 0; padding: 0 1em; color: #59636e; }
table { border-collapse: collapse; } th, td { border: 1px solid #d0d7de; padding: 6px 12px; }
tr:nth-child(2n) { background: #f6f8fa; } img { max-width: 100%; }
"""


def _render_html_string(html: str, base_dir: Path, output: Path, **opts):
    """Render an HTML string; relative links (images…) resolve against base_dir."""
    html = html.replace("<head>", f"<head><base href='{base_dir.resolve().as_uri()}/'>", 1)
    with tempdir() as tmp:
        page = tmp / f"{output.stem}.html"
        page.write_text(html, encoding="utf-8")
        return html_to_pdf([str(page)], output, **opts)


@tool(
    name="markdown-to-pdf", title="Markdown to PDF", category="Extras", extra=True,
    help="Render Markdown (.md) with GitHub-like styling, tables and code blocks into a PDF.",
    aliases=("md-to-pdf",), accept=(".md", ".markdown", ".mdown"),
    opts=[o for o in HTML_OPTS if o.name in ("paper", "orientation", "margin", "engine")],
)
def markdown_to_pdf(inputs, output: Path, **opts):
    import markdown

    src = Path(inputs[0])
    text = src.read_text(encoding="utf-8")
    # GitHub task lists: "- [ ] todo" / "- [x] done"
    text = re.sub(r"^(\s*[-*+]\s+)\[ \]", r"\1☐", text, flags=re.M)
    text = re.sub(r"^(\s*[-*+]\s+)\[[xX]\]", r"\1☑", text, flags=re.M)
    body = markdown.markdown(text,
                             extensions=["extra", "sane_lists", "toc", "codehilite", "admonition"],
                             extension_configs={"codehilite": {"noclasses": True}})
    html = f"<!doctype html><html><head><meta charset='utf-8'><title>{html_lib.escape(src.stem)}</title>" \
           f"<style>{MD_CSS}</style></head><body>{body}</body></html>"
    return _render_html_string(html, src.parent, output, **opts)


@tool(
    name="text-to-pdf", title="Text to PDF", category="Extras", extra=True,
    help="Turn plain text, logs or source code into a clean, monospaced PDF.",
    aliases=("txt-to-pdf",), accept=(".txt", ".log", ".csv", ".py", ".sh", ".c", ".js", ".json", ".yaml", ".yml", ".ini", ".conf"),
    opts=[
        Opt("font_size", "float", 9.5, "Font size in points."),
        Opt("paper", "choice", "a4", "Page size.", ["a4", "letter", "legal", "a3", "a5"]),
        Opt("orientation", "choice", "portrait", "Page orientation.", ["portrait", "landscape"]),
        Opt("line_numbers", "bool", False, "Prefix every line with its number."),
    ],
)
def text_to_pdf(inputs, output: Path, font_size=9.5, paper="a4", orientation="portrait", line_numbers=False):
    src = Path(inputs[0])
    lines = src.read_text(encoding="utf-8", errors="replace").expandtabs(4).splitlines()
    indent = 4  # wrapped continuation lines get a hanging indent so they don't look like new lines
    if line_numbers:
        width = len(str(len(lines)))
        lines = [f"{i:>{width}}  {line}" for i, line in enumerate(lines, 1)]
        indent = width + 2
    html = "".join(f"<p>{html_lib.escape(line) or '&nbsp;'}</p>" for line in lines)
    em = indent * 0.6  # monospace advance ≈ 0.6 em
    css = (f"p {{ font-family: monospace; font-size: {font_size}pt; white-space: pre-wrap; margin: 0; "
           f"padding-left: {em:.2f}em; text-indent: -{em:.2f}em; }}")
    _story_to_pdf(html, output, paper, orientation, 40, css=css)
    return [output]
