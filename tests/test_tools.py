"""End-to-end tests: every tool is run on real files and its output is checked."""
from __future__ import annotations

import json
import unicodedata
import zipfile
from pathlib import Path

import pymupdf as fitz
import pytest

from localpdf.cli import main
from localpdf.registry import get, load_all
from localpdf.runner import run_tool
from localpdf.utils import ToolError, parse_pages, which


def run(tool_name, inputs, out=None, **opts):
    return run_tool(get(tool_name), [str(i) for i in inputs], out, opts)


def text_of(path) -> str:
    return unicodedata.normalize("NFKC", "".join(p.get_text() for p in fitz.open(path)))  # NFKC: "ﬁ" → "fi"


needs_soffice = pytest.mark.skipif(not which("soffice", "libreoffice"), reason="LibreOffice not installed")
needs_gs = pytest.mark.skipif(not which("gs"), reason="Ghostscript not installed")
needs_tesseract = pytest.mark.skipif(not which("tesseract"), reason="Tesseract not installed")


# ------------------------------------------------------------------ utils
def test_parse_pages():
    assert parse_pages("all", 5) == [0, 1, 2, 3, 4]
    assert parse_pages("1,3-4", 5) == [0, 2, 3]
    assert parse_pages("3-", 5) == [2, 3, 4]
    assert parse_pages("-2", 5) == [0, 1]
    assert parse_pages("5-3", 5) == [4, 3, 2]
    assert parse_pages("odd", 5) == [0, 2, 4]
    assert parse_pages("even", 5) == [1, 3]
    assert parse_pages("last,r2", 5) == [4, 3]
    with pytest.raises(ToolError):
        parse_pages("9", 5)


def test_every_tool_has_cli_parser():
    tools = load_all()
    assert len(tools) >= 50
    for name in tools:
        with pytest.raises(SystemExit) as e:
            main([name, "--help"])
        assert e.value.code == 0


# ------------------------------------------------------------------ organize
def test_merge(samples, tmp_path):
    out = tmp_path / "m.pdf"
    run("merge", [samples["pdf"], samples["short"], samples["photo1.jpg"]], out)
    doc = fitz.open(out)
    assert doc.page_count == 6 + 3 + 1
    assert [t[1] for t in doc.get_toc() if t[0] == 1] == ["report", "short", "photo1"]


def test_split_modes(samples, tmp_path):
    assert len(run("split", [samples["pdf"]], tmp_path / "a")) == 6
    parts = run("split", [samples["pdf"]], tmp_path / "b", mode="ranges", ranges="1-2;3-6")
    assert [fitz.open(p).page_count for p in parts] == [2, 4]
    assert len(run("split", [samples["pdf"]], tmp_path / "c", mode="every", every=4)) == 2
    assert len(run("split", [samples["pdf"]], tmp_path / "d", mode="bookmarks")) == 6
    assert len(run("split", [samples["pdf"]], tmp_path / "e", mode="size", max_size=0.002)) >= 2


def test_remove_extract_organize(samples, tmp_path):
    out = run("remove-pages", [samples["pdf"]], tmp_path / "r.pdf", pages="2-5")[0]
    assert fitz.open(out).page_count == 2
    out = run("extract-pages", [samples["pdf"]], tmp_path / "x.pdf", pages="odd")[0]
    assert fitz.open(out).page_count == 3
    out = run("organize", [samples["pdf"]], tmp_path / "o.pdf", order="3,1,blank,2:90")[0]
    doc = fitz.open(out)
    assert doc.page_count == 4
    assert "Chapter 3" in doc[0].get_text() and not doc[2].get_text().strip()
    assert doc[3].rotation == 90


def test_scan_to_pdf(samples, tmp_path):
    out = run("scan-to-pdf", [samples["photo1.jpg"], samples["photo2.png"]], tmp_path / "s.pdf", mode="bw")[0]
    assert fitz.open(out).page_count == 2


def test_extras_organize(samples, tmp_path):
    rev = run("reverse", [samples["pdf"]], tmp_path / "rev.pdf")[0]
    assert "Chapter 6" in fitz.open(rev)[0].get_text()
    inter = run("interleave", [samples["pdf"], samples["short"]], tmp_path / "i.pdf")[0]
    assert fitz.open(inter).page_count == 9
    assert "Short document" in fitz.open(inter)[1].get_text()
    nup = run("n-up", [samples["pdf"]], tmp_path / "n.pdf", per_sheet="4")[0]
    assert fitz.open(nup).page_count == 2
    rs = run("resize", [samples["short"]], tmp_path / "rs.pdf", paper="letter")[0]
    assert round(fitz.open(rs)[0].rect.width) == 612
    rb = run("remove-blank", [samples["short"]], tmp_path / "rb.pdf")[0]
    assert fitz.open(rb).page_count == 2
    ib = run("insert-blank", [samples["short"]], tmp_path / "ib.pdf", after="1")[0]
    assert fitz.open(ib).page_count == 4
    bk = run("booklet", [samples["pdf"]], tmp_path / "bk.pdf")[0]
    assert fitz.open(bk).page_count == 4  # 8 slots → 2 sheets × 2 sides


# ------------------------------------------------------------------ optimize
def test_compress(samples, tmp_path):
    out = run("compress", [samples["pdf"]], tmp_path / "c.pdf", level="extreme")[0]
    assert out.stat().st_size <= samples["pdf"].stat().st_size
    assert "Chapter 1" in text_of(out)


def test_repair(samples, tmp_path):
    broken = tmp_path / "broken.pdf"
    data = samples["pdf"].read_bytes()
    broken.write_bytes(data[: data.rfind(b"xref")])  # chop the xref table + trailer
    out = run("repair", [broken], tmp_path / "fixed.pdf")[0]
    assert fitz.open(out).page_count == 6


@needs_tesseract
def test_ocr(samples, tmp_path):
    assert not text_of(samples["scanned"]).strip()
    out = run("ocr", [samples["scanned"]], tmp_path / "o.pdf")[0]
    assert "12345" in text_of(out)
    # the scan itself is kept as is: only an invisible text layer is added
    assert out.stat().st_size < samples["scanned"].stat().st_size * 1.5
    forced = run("ocr", [samples["scanned"]], tmp_path / "f.pdf", force=True, dpi=200)[0]
    assert "12345" in text_of(forced)


# ------------------------------------------------------------------ convert to
def test_jpg_to_pdf(samples, tmp_path):
    out = run("jpg-to-pdf", [samples["photo1.jpg"], samples["photo2.png"]], tmp_path / "j.pdf", page_size="a4", margin="small")[0]
    doc = fitz.open(out)
    assert doc.page_count == 2 and round(doc[0].rect.width) == 595


@needs_soffice
@pytest.mark.parametrize("tool,key,needle", [("word-to-pdf", "docx", "Paragraph converted"),
                                              ("excel-to-pdf", "xlsx", "Apples"),
                                              ("powerpoint-to-pdf", "pptx", "Slide title")])
def test_office_to_pdf(samples, tmp_path, tool, key, needle):
    out = run(tool, [samples[key]], tmp_path / f"{key}.pdf")[0]
    assert needle in text_of(out)


@pytest.mark.parametrize("engine", ["auto", "weasyprint", "mupdf"])
def test_html_file_to_pdf(samples, tmp_path, engine):
    pytest.importorskip("weasyprint") if engine == "weasyprint" else None
    out = run("html-to-pdf", [samples["html"]], tmp_path / f"h_{engine}.pdf", engine=engine)[0]
    txt = text_of(out)
    assert "Hello from a local HTML file" in txt and "Styled box" in txt
    doc = fitz.open(out)
    if engine != "mupdf":
        assert doc[0].get_images(), "relative <img> should be embedded"


def _chromium_or_skip(fn):
    try:
        return fn()
    except ToolError as e:
        if "no HTML engine" in str(e) or "Chromium" in str(e):
            pytest.skip(f"Chromium unavailable: {e}")
        raise


def test_html_chromium_runs_javascript(samples, tmp_path):
    pytest.importorskip("playwright")
    out = _chromium_or_skip(lambda: run("html-to-pdf", [samples["html"]], tmp_path / "c.pdf", engine="chromium")[0])
    txt = text_of(out)
    assert "JavaScript ran" in txt and fitz.open(out)[0].get_images()
    single = run("html-to-pdf", [samples["html"]], tmp_path / "s.pdf", engine="chromium", paper="single")[0]
    assert fitz.open(single).page_count == 1


def test_html_from_url(samples, tmp_path):
    import functools
    import http.server
    import threading

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(samples["html"].parent))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/index.html"
        out = run_tool(get("html-to-pdf"), [url], tmp_path / "u.pdf", {})[0]
        assert "Hello from a local HTML file" in text_of(out)
    finally:
        server.shutdown()


def test_html_folder_and_zip(samples, tmp_path):
    site = samples["html"].parent
    out = run("html-to-pdf", [site], tmp_path / "folder.pdf")[0]
    assert "Hello from a local HTML file" in text_of(out)
    z = tmp_path / "site.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for f in site.rglob("*"):
            zf.write(f, f.relative_to(site))
    out = run("html-to-pdf", [z], tmp_path / "zip.pdf")[0]
    assert "Styled box" in text_of(out)


def test_markdown_and_text(samples, tmp_path):
    out = run("markdown-to-pdf", [samples["md"]], tmp_path / "md.pdf")[0]
    assert "Notes" in text_of(out) and "print" in text_of(out)
    out = run("text-to-pdf", [samples["txt"]], tmp_path / "t.pdf", line_numbers=True)[0]
    doc = fitz.open(out)
    assert doc.page_count > 1 and "line 199" in text_of(out)


# ------------------------------------------------------------------ convert from
def test_pdf_to_jpg(samples, tmp_path):
    files = run("pdf-to-jpg", [samples["pdf"]], tmp_path / "jpg", dpi=50)
    assert len(files) == 6 and files[0].suffix == ".jpg"
    files = run("pdf-to-jpg", [samples["pdf"]], tmp_path / "png", format="png", pages="1")
    assert len(files) == 1 and files[0].suffix == ".png"
    imgs = run("pdf-to-jpg", [samples["pdf"]], tmp_path / "ext", mode="extract")
    assert len(imgs) == 1


def test_pdf_to_word(samples, tmp_path):
    pytest.importorskip("pdf2docx")
    from docx import Document

    out = run("pdf-to-word", [samples["pdf"]], tmp_path / "w.docx", pages="1-2")[0]
    text = "\n".join(p.text for p in Document(out).paragraphs)
    assert "Chapter 1" in text


def test_pdf_to_powerpoint(samples, tmp_path):
    from pptx import Presentation

    out = run("pdf-to-powerpoint", [samples["pdf"]], tmp_path / "p.pptx", dpi=50)[0]
    prs = Presentation(out)
    assert len(prs.slides) == 6
    assert "Chapter 1" in prs.slides[0].notes_slide.notes_text_frame.text


def test_pdf_to_excel(samples, tmp_path):
    from openpyxl import load_workbook

    out = run("pdf-to-excel", [samples["pdf"]], tmp_path / "x.xlsx", pages="2", text=False)[0]
    wb = load_workbook(out)
    values = [c for ws in wb for row in ws.iter_rows(values_only=True) for c in row]
    assert "North" in values and 1350 in values and 1620 in values


@needs_gs
def test_pdf_to_pdfa(samples, tmp_path):
    out = run("pdf-to-pdfa", [samples["short"]], tmp_path / "a.pdf")[0]
    doc = fitz.open(out)
    assert "pdfaid" in (doc.get_xml_metadata() or "")


def test_pdf_to_text_html_md(samples, tmp_path):
    t = run("pdf-to-text", [samples["pdf"]], tmp_path / "t.txt")[0]
    assert "Chapter 6" in t.read_text()
    h = run("pdf-to-html", [samples["pdf"]], tmp_path / "t.html")[0]
    assert "Chapter" in h.read_text()
    m = run("pdf-to-markdown", [samples["pdf"]], tmp_path / "t.md")[0]
    assert "Chapter 1" in m.read_text()


# ------------------------------------------------------------------ edit
def test_rotate_batch(samples, tmp_path):
    outs = run("rotate", [samples["pdf"], samples["short"]], tmp_path / "rot", angle="180")
    assert len(outs) == 2 and all(fitz.open(o)[0].rotation == 180 for o in outs)


def test_page_numbers(samples, tmp_path):
    out = run("page-numbers", [samples["pdf"]], tmp_path / "n.pdf", format="Page {n} of {total}", pages="2-", start=1)[0]
    doc = fitz.open(out)
    assert "Page 1 of 5" in doc[1].get_text() and "Page" not in doc[0].get_text().replace("Chapter", "")


def test_page_numbers_on_rotated_page(samples, tmp_path):
    rot = run("rotate", [samples["short"]], tmp_path / "r.pdf", angle="90")[0]
    out = run("page-numbers", [rot], tmp_path / "rn.pdf", position="bottom-right")[0]
    page = fitz.open(out)[0]
    words = [w for w in page.get_text("words") if w[4] == "1"]
    assert words, "number must be present"
    # text extraction is in unrotated coordinates – map to what the reader actually sees
    visible = fitz.Rect(words[-1][:4]) * page.rotation_matrix
    assert visible.x1 > page.rect.width * 0.8 and visible.y1 > page.rect.height * 0.8, "bottom-right as displayed"


def _visible_direction(page, needle):
    """Writing direction of `needle` as the reader sees it (text extraction is in unrotated space)."""
    m = page.rotation_matrix
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            if needle in "".join(s["text"] for s in line["spans"]):
                d = fitz.Point(line["dir"]) * m - fitz.Point(0, 0) * m
                return round(d.x, 2), round(d.y, 2)
    raise AssertionError(f"{needle!r} not found")


@pytest.mark.parametrize("angle", ["90", "180", "270"])
def test_text_is_upright_on_rotated_pages(samples, tmp_path, angle):
    rot = run("rotate", [samples["short"]], tmp_path / "r.pdf", angle=angle)[0]
    out = run("page-numbers", [rot], tmp_path / "n.pdf", format="Page {n} of {total}")[0]
    out = run("watermark", [out], tmp_path / "w.pdf", text="WATERMARK", rotation=0)[0]
    out = run("sign", [out], tmp_path / "s.pdf", name="Jane Doe", page="1")[0]
    ops = '[{"type": "text", "page": 1, "x": 72, "y": 72, "text": "EditedText"}]'
    out = run("edit", [out], tmp_path / "e.pdf", ops=ops)[0]
    page = fitz.open(out)[0]
    for needle in ("Page 1 of 3", "WATERMARK", "Jane Doe", "Signed", "EditedText"):
        assert _visible_direction(page, needle) == (1, 0), needle
    # and it lands where asked: page number at the visible bottom, watermark centred
    def visible(word):
        return fitz.Rect(next(w for w in page.get_text("words") if w[4] == word)[:4]) * page.rotation_matrix

    assert visible("Page").y1 > page.rect.height * 0.85
    mark = visible("WATERMARK")
    assert abs((mark.x0 + mark.x1) / 2 - page.rect.width / 2) < 5 and abs((mark.y0 + mark.y1) / 2 - page.rect.height / 2) < 30


def test_watermark(samples, tmp_path):
    out = run("watermark", [samples["pdf"]], tmp_path / "w.pdf", text="CONFIDENTIAL", position="tile")[0]
    assert "CONFIDENTIAL" in fitz.open(out)[0].get_text()
    out = run("watermark", [samples["pdf"]], tmp_path / "wi.pdf", image=str(samples["photo2.png"]), opacity=0.2)[0]
    assert len(fitz.open(out)[0].get_images()) >= 1


def test_crop(samples, tmp_path):
    out = run("crop", [samples["pdf"]], tmp_path / "c.pdf", margins="20")[0]
    w = fitz.open(out)[0].rect.width
    assert abs(w - (595 - 2 * 20 * 72 / 25.4)) < 1
    out = run("crop", [samples["short"]], tmp_path / "ca.pdf", auto=True, pages="1")[0]
    assert fitz.open(out)[0].rect.width < 300


def test_edit(samples, tmp_path):
    ops = json.dumps([
        {"type": "text", "page": 1, "x": 72, "y": 50, "text": "Inserted note", "color": "#e5322d"},
        {"type": "rect", "page": 1, "rect": [60, 60, 200, 100], "color": "blue"},
        {"type": "highlight", "page": "all", "search": "budget"},
        {"type": "image", "page": 2, "rect": [300, 600, 400, 700], "file": str(samples["photo1.jpg"])},
        {"type": "link", "page": 1, "rect": [72, 700, 200, 720], "url": "https://example.com"},
    ])
    out = run("edit", [samples["pdf"]], tmp_path / "e.pdf", ops=ops, find="Results", replace="Findings")[0]
    doc = fitz.open(out)
    assert "Inserted note" in doc[0].get_text()
    assert "Findings" in doc[0].get_text() and "Results" not in doc[0].get_text()
    assert any(a.type[1] == "Highlight" for a in doc[0].annots())
    assert doc[0].get_links()[0]["uri"] == "https://example.com"


# ------------------------------------------------------------------ security
def test_protect_unlock(samples, tmp_path):
    locked = run("protect", [samples["short"]], tmp_path / "l.pdf", password="s3cret", allow="print")[0]
    doc = fitz.open(locked)
    assert doc.needs_pass and doc.authenticate("s3cret")
    with pytest.raises(ToolError):
        run("unlock", [locked], tmp_path / "u0.pdf", password="wrong")
    unlocked = run("unlock", [locked], tmp_path / "u.pdf", password="s3cret")[0]
    assert not fitz.open(unlocked).is_encrypted


def test_sign(samples, tmp_path):
    out = run("sign", [samples["pdf"]], tmp_path / "s.pdf", name="Jane Doe", initials="JD")[0]
    doc = fitz.open(out)
    assert "Jane Doe" in doc[-1].get_text() and "Signed" in doc[-1].get_text()
    assert "JD" in doc[0].get_text()
    out = run("sign", [samples["short"]], tmp_path / "si.pdf", image=str(samples["signature"]), page="1", date=False)[0]
    assert fitz.open(out)[0].get_images()


def test_sign_with_certificate(samples, tmp_path):
    pytest.importorskip("pyhanko")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    import datetime as dt

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Signer")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    p12 = tmp_path / "me.p12"
    p12.write_bytes(pkcs12.serialize_key_and_certificates(b"me", key, cert, None,
                                                          serialization.BestAvailableEncryption(b"pw")))
    out = run("sign", [samples["short"]], tmp_path / "ds.pdf", name="Test Signer", cert=str(p12), cert_password="pw",
              reason="Approval")[0]
    from pyhanko.pdf_utils.reader import PdfFileReader

    with open(out, "rb") as fh:
        sigs = PdfFileReader(fh).embedded_signatures
        assert len(sigs) == 1 and sigs[0].field_name == "Signature1"


def test_redact(samples, tmp_path):
    out = run("redact", [samples["pdf"]], tmp_path / "r.pdf", text="budget", presets="email,phone")[0]
    txt = text_of(out)
    assert "budget" not in txt and "alice@example.com" not in txt and "555 123" not in txt
    assert "Chapter 1" in txt
    assert not fitz.open(out).metadata.get("author")


def test_compare(samples, tmp_path, capsys):
    out = run("compare", [samples["pdf"], samples["pdf2"]], tmp_path / "cmp.pdf")[0]
    assert fitz.open(out).page_count == 6
    printed = capsys.readouterr().out
    assert "revenue" in printed and "difference" in printed


# ------------------------------------------------------------------ intelligence & extras
def test_summarize(samples, tmp_path):
    out = run("summarize", [samples["pdf"]], tmp_path / "s.md", length="short")[0]
    md = out.read_text()
    assert md.startswith("# Summary") and md.count("\n- ") >= 3


def test_translate_needs_engine(samples, tmp_path):
    from localpdf.tools import ai

    # plug a fake translator in to exercise the layout-preserving pipeline offline
    orig = ai._translator
    ai._translator = lambda *a: (lambda text: text.upper())
    try:
        out = run("translate", [samples["short"]], tmp_path / "t.pdf", to="fr", source="en")[0]
    finally:
        ai._translator = orig
    assert "SHORT DOCUMENT" in text_of(out)


def test_info_metadata_flatten_grayscale(samples, tmp_path):
    report = run("info", [samples["pdf"]], json=True)
    data = json.loads(report)
    assert data["pages"] == 6 and data["bookmarks"] == 6 and not data["looks_scanned"]
    assert json.loads(run("info", [samples["scanned"]], json=True))["looks_scanned"]
    m = run("metadata", [samples["pdf"]], tmp_path / "m.pdf", title="New title", author="Me")[0]
    assert fitz.open(m).metadata["title"] == "New title"
    m2 = run("metadata", [m], tmp_path / "m2.pdf", clear=True)[0]
    assert not fitz.open(m2).metadata.get("author")
    f = run("flatten", [samples["form"]], tmp_path / "f.pdf")[0]
    assert not list(fitz.open(f)[0].widgets())
    g = run("grayscale", [samples["pdf"]], tmp_path / "g.pdf")[0]
    assert fitz.open(g).page_count == 6


def test_fill_form(samples, tmp_path):
    listing = json.loads(run("fill-form", [samples["form"]]))
    assert set(listing) == {"name", "agree"}
    out = run("fill-form", [samples["form"]], tmp_path / "ff.pdf", data='{"name": "Ada", "agree": true}')[0]
    values = {w.field_name: w.field_value for w in fitz.open(out)[0].widgets()}
    assert values["name"] == "Ada" and values["agree"] not in ("Off", False)


def test_attachments_and_bookmarks(samples, tmp_path):
    files = run("attachments", [samples["short"], samples["txt"]], tmp_path / "att")
    pdf = files[0]
    assert fitz.open(pdf).embfile_names() == ["log.txt"]
    extracted = run("attachments", [pdf], tmp_path / "ext")
    assert extracted[0].read_text().startswith("line 0")
    out = run("bookmarks", [samples["short"]], tmp_path / "b.pdf", set='[[1, "Intro", 1], [1, "End", 3]]')[0]
    assert fitz.open(out).get_toc()[1][1] == "End"
    auto = run("bookmarks", [samples["pdf"]], tmp_path / "ba.pdf", auto=True)[0]
    assert len(fitz.open(auto).get_toc()) == 6


def test_cli_end_to_end(samples, tmp_path, capsys):
    out = tmp_path / "cli.pdf"
    assert main(["merge", str(samples["short"]), str(samples["short"]), "-o", str(out)]) == 0
    assert fitz.open(out).page_count == 6
    assert main(["remove-pages", str(out), "--pages", "99"]) == 1
    assert "out of range" in capsys.readouterr().err


def test_web_api(samples):
    from localpdf.web import create_app

    client = create_app().test_client()
    tools = client.get("/api/tools").get_json()
    assert len(tools["tools"]) >= 50
    assert client.get("/").status_code == 200
    with open(samples["short"], "rb") as a, open(samples["pdf"], "rb") as b:
        r = client.post("/api/run/merge", data={"files": [(a, "a.pdf"), (b, "b.pdf")], "bookmarks": "true"},
                        content_type="multipart/form-data")
    assert r.status_code == 200 and r.mimetype == "application/pdf"
    assert fitz.open("pdf", r.data).page_count == 9
    with open(samples["pdf"], "rb") as a:
        r = client.post("/api/run/split", data={"files": [(a, "a.pdf")]}, content_type="multipart/form-data")
    assert r.mimetype == "application/zip" and len(zipfile.ZipFile(__import__("io").BytesIO(r.data)).namelist()) == 6
    with open(samples["pdf"], "rb") as a:
        r = client.post("/api/run/info", data={"files": [(a, "a.pdf")]}, content_type="multipart/form-data")
    assert "pages: 6" in r.get_json()["text"]
    with open(samples["pdf"], "rb") as a:
        r = client.post("/api/run/remove-pages", data={"files": [(a, "a.pdf")], "pages": "77"},
                        content_type="multipart/form-data")
    assert r.status_code == 400 and "out of range" in r.get_json()["error"]


# ------------------------------------------------------------------ regressions found by examples/ evaluation
REALWORLD = Path(__file__).parent.parent / "examples" / "inputs" / "realworld"


def test_number_parsing():
    from localpdf.tools.convert_from import _number

    assert _number("18,870") == (18870, "#,##0")
    assert _number("98 452") == (98452, "#,##0")  # space thousands separator
    assert _number("3,873.52 €") == (3873.52, '#,##0.00 "€"')
    assert _number("$1,200.00") == (1200.0, '"$"#,##0.00')
    assert _number("25.1%") == (0.251, "0.0%")
    assert _number("(1,234.00)") == (-1234.0, "#,##0.00")
    assert _number("007") == ("007", None) and _number("08/12/2019") == ("08/12/2019", None)
    assert _number("") == (None, None)


@pytest.mark.skipif(not REALWORLD.exists(), reason="examples not present")
def test_tables_on_real_world_pdfs():
    from localpdf.tables import extract_tables

    nics = extract_tables(fitz.open(REALWORLD / "nics-background-checks-2015-11.pdf")[0])[0]
    rows = {r[0]: r for r in nics}
    assert rows["Alabama"][1:4] == ["18,870", "23,022", "22,650"]  # one state per row, commas intact
    assert rows["Totals"][1] == "804,006"
    # the Senate ledger is drawn rotated 90° and in fake bold (every glyph printed twice)
    senate = extract_tables(fitz.open(REALWORLD / "senate-expenditures.pdf")[0])[0]
    row = next(r for r in senate if r[0] == "DHAW20190001")
    assert "05/03/2019" in row and "920.68" in row and "STAFF TRANSPORTATION" in " ".join(row)


def test_pdf_to_excel_keeps_text_and_totals(samples, tmp_path):
    from openpyxl import load_workbook

    out = run("pdf-to-excel", [samples["pdf"]], tmp_path / "x.xlsx", pages="2")[0]
    values = [c for row in load_workbook(out).active.iter_rows(values_only=True) for c in row if c is not None]
    assert "Chapter 2: Results" in values  # text around the table is kept (sheet-per-page)
    assert {"North", 1350, 1620} <= set(values)


def test_reading_order_multi_column(tmp_path):
    from localpdf.utils import reading_order

    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(40, 40, 555, 80), "TITLE spanning the page", fontsize=16)
    for col, x in enumerate((40, 310)):
        for k in range(4):  # paragraphs whose gaps line up across columns
            page.insert_textbox(fitz.Rect(x, 100 + k * 60, x + 245, 150 + k * 60), f"col{col} para{k} " * 4, fontsize=9)
    text = " ".join(reading_order(page))
    order = [text.index(f"col{c} para{k}") for c in range(2) for k in range(4)]
    assert text.index("TITLE") < order[0] and order == sorted(order)


def test_scan_deskew_angle():
    from PIL import Image, ImageDraw

    from localpdf.tools.organize import _skew_angle

    img = Image.new("L", (1200, 1600), 255)
    d = ImageDraw.Draw(img)
    for i in range(30):
        d.text((100, 100 + i * 45), "The quick brown fox jumps over the lazy dog " * 2, fill=0)
    tilted = img.rotate(4, expand=True, fillcolor=255)
    assert abs(_skew_angle(tilted) + 4) < 0.6


@needs_soffice
def test_excel_fit_width(tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append([f"column {i}" for i in range(14)])
    for r in range(30):
        ws.append([f"value {r}-{i}" for i in range(14)])
    for i in range(14):
        ws.column_dimensions[chr(65 + i)].width = 18
    src = tmp_path / "wide.xlsx"
    wb.save(src)
    narrow = run("excel-to-pdf", [src], tmp_path / "none.pdf", fit="none")[0]
    fitted = run("excel-to-pdf", [src], tmp_path / "w.pdf")[0]
    assert "column 13" not in fitz.open(narrow)[0].get_text()  # LibreOffice default cuts the columns
    assert "column 13" in fitz.open(fitted)[0].get_text() and fitz.open(fitted).page_count == 1


def test_jpg_to_pdf_bogus_dpi(tmp_path):
    from PIL import Image

    p = tmp_path / "fax.tiff"
    frames = [Image.new("1", (1240, 1754), 1) for _ in range(2)]
    frames[0].save(p, save_all=True, append_images=frames[1:], dpi=(1, 1))
    doc = fitz.open(run("jpg-to-pdf", [p], tmp_path / "f.pdf")[0])
    assert doc.page_count == 2 and doc[0].rect.width < 1000


def test_html_single_page_is_one_page(samples, tmp_path):
    pytest.importorskip("playwright")
    long_page = tmp_path / "long.html"  # lots of wrapping text: any width mismatch makes it taller
    long_page.write_text("<body style='font:16px sans-serif'>" + ("<p>" + "word " * 400 + "</p>") * 12 + "</body>")
    out = _chromium_or_skip(lambda: run("html-to-pdf", [long_page], tmp_path / "s.pdf", engine="chromium",
                                        paper="single", margin="big")[0])
    assert fitz.open(out).page_count == 1


# ------------------------------------------------------------------ HTML documents designed as sheets of paper
SHEET_CSS = """
body{margin:0;background:#ccc;font:16px sans-serif}
.page{width:210mm;margin:8mm auto}
.sheet{background:#fff;width:210mm;min-height:297mm;box-shadow:0 4px 20px #0005;border-radius:6px}
.cols{display:grid;grid-template-columns:60mm 1fr}
aside{background:#eef;padding:6mm}
main{padding:6mm}
@media (max-width:230mm){.cols{grid-template-columns:1fr}}   /* the trap: A4 is narrower than 230 mm */
"""


def _sheet_html(tmp_path, paragraphs=6, sheets=1):
    body = "".join(
        f"<div class='page'><div class='sheet'><div class='cols'><aside>SIDEBAR{n} <a href='https://example.org/{n}'>"
        f"link</a></aside><main>MAIN{n} " + "<p>" + ("lorem ipsum dolor sit amet " * 40 + "</p><p>") * paragraphs + "</p>"
        + "</main></div></div></div>" for n in range(sheets))
    p = tmp_path / "sheet.html"
    p.write_text(f"<!doctype html><html><head><style>{SHEET_CSS}</style></head><body>{body}</body></html>")
    return p


def _desktop_layout(page, n=0):
    """Sidebar beside the main column (two-column layout), not stacked above it."""
    words = {w[4]: fitz.Rect(w[:4]) for w in page.get_text("words")}
    side, main = words[f"SIDEBAR{n}"], words[f"MAIN{n}"]
    return side.x1 < main.x0 and abs(side.y0 - main.y0) < 30


def test_html_sheet_rendered_like_on_screen(tmp_path):
    pytest.importorskip("playwright")
    src = _sheet_html(tmp_path, paragraphs=2)
    out = _chromium_or_skip(lambda: run("html-to-pdf", [src], tmp_path / "a.pdf", engine="chromium")[0])
    doc = fitz.open(out)
    page = doc[0]
    assert doc.page_count == 1 and (round(page.rect.width / 72 * 25.4), round(page.rect.height / 72 * 25.4)) == (210, 297)
    assert _desktop_layout(page)
    assert page.get_links()[0]["uri"] == "https://example.org/0"
    # the old behaviour is still one switch away, and it falls into the responsive trap
    off = fitz.open(run("html-to-pdf", [src], tmp_path / "off.pdf", engine="chromium", sheets="off")[0])
    assert not _desktop_layout(off[0])


def test_html_overflowing_sheet_shrink_or_grow(tmp_path):
    pytest.importorskip("playwright")
    src = _sheet_html(tmp_path, paragraphs=12)  # far more text than one A4 page holds
    shrunk = fitz.open(_chromium_or_skip(lambda: run("html-to-pdf", [src], tmp_path / "s.pdf", engine="chromium")[0]))
    assert shrunk.page_count == 1 and round(shrunk[0].rect.height / 72 * 25.4) == 297
    assert _desktop_layout(shrunk[0]) and shrunk[0].get_links()[0]["uri"] == "https://example.org/0"
    grown = fitz.open(run("html-to-pdf", [src], tmp_path / "g.pdf", engine="chromium", sheet_fit="grow")[0])
    assert grown.page_count == 1 and grown[0].rect.height / 72 * 25.4 > 300 and round(grown[0].rect.width / 72 * 25.4) == 210


def test_html_several_sheets_and_selector(tmp_path):
    pytest.importorskip("playwright")
    src = _sheet_html(tmp_path, paragraphs=2, sheets=3)
    doc = fitz.open(_chromium_or_skip(lambda: run("html-to-pdf", [src], tmp_path / "m.pdf", engine="chromium")[0]))
    assert doc.page_count == 3 and all(_desktop_layout(doc[i], i) for i in range(3))
    by_selector = fitz.open(run("html-to-pdf", [src], tmp_path / "sel.pdf", engine="chromium", sheets=".sheet")[0])
    assert by_selector.page_count == 3
    with pytest.raises(ToolError, match="no element matches"):
        run("html-to-pdf", [src], tmp_path / "x.pdf", engine="chromium", sheets=".missing")
