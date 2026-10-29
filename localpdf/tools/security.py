"""PDF Security: protect, unlock, sign, redact, compare."""
from __future__ import annotations

import datetime as dt
import difflib
import io
import re
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import POSITIONS, ToolError, open_pdf, parse_pages, place, save_pdf
from ..utils import color as parse_color

PERMISSIONS = ["print", "print-hq", "copy", "modify", "annotate", "forms", "assemble", "accessibility"]


@tool(
    name="protect", title="Protect PDF", category="PDF Security",
    help="Encrypt your PDF with a password (AES-256) to prevent unauthorized access.",
    aliases=("encrypt",), requires=("pikepdf",),
    opts=[
        Opt("password", "str", None, "Password required to open the file.", required=True),
        Opt("owner_password", "str", None, "Separate owner password for changing permissions (default: random)."),
        Opt("allow", "str", "print,print-hq,copy,annotate,forms,accessibility,assemble,modify",
            f"Comma list of allowed actions: {', '.join(PERMISSIONS)}, or 'none'."),
        Opt("encryption", "choice", "aes-256", "Encryption algorithm.", ["aes-256", "aes-128", "rc4-128"]),
    ],
)
def protect(inputs, output: Path, password, owner_password=None, allow=None, encryption="aes-256"):
    import secrets

    allowed = set() if (allow or "").strip() == "none" else {a.strip() for a in (allow or ",".join(PERMISSIONS)).split(",") if a.strip()}
    unknown = allowed - set(PERMISSIONS)
    if unknown:
        raise ToolError(f"unknown permission(s): {', '.join(sorted(unknown))}")
    owner = owner_password or secrets.token_urlsafe(24)
    try:
        import pikepdf
    except ImportError:
        return _protect_mupdf(inputs, output, password, owner, allowed, encryption)
    perms = pikepdf.Permissions(
        print_lowres="print" in allowed or "print-hq" in allowed, print_highres="print-hq" in allowed,
        extract="copy" in allowed, modify_other="modify" in allowed, modify_annotation="annotate" in allowed,
        modify_form="forms" in allowed, modify_assembly="assemble" in allowed, accessibility="accessibility" in allowed,
    )
    R, aes = {"aes-256": (6, True), "aes-128": (4, True), "rc4-128": (3, False)}[encryption]
    with pikepdf.open(inputs[0]) as pdf:
        pdf.save(output, encryption=pikepdf.Encryption(user=password, owner=owner, R=R, aes=aes, allow=perms))
    return [output]


def _protect_mupdf(inputs, output, password, owner, allowed, encryption):
    method = {"aes-256": fitz.PDF_ENCRYPT_AES_256, "aes-128": fitz.PDF_ENCRYPT_AES_128, "rc4-128": fitz.PDF_ENCRYPT_RC4_128}[encryption]
    flags = {"print": fitz.PDF_PERM_PRINT, "print-hq": fitz.PDF_PERM_PRINT_HQ, "copy": fitz.PDF_PERM_COPY,
             "modify": fitz.PDF_PERM_MODIFY, "annotate": fitz.PDF_PERM_ANNOTATE, "forms": fitz.PDF_PERM_FORM,
             "assemble": fitz.PDF_PERM_ASSEMBLE, "accessibility": fitz.PDF_PERM_ACCESSIBILITY}
    perm = 0
    for a in allowed:
        perm |= flags[a]
    doc = open_pdf(inputs[0])
    return save_pdf(doc, output, encryption=method, user_pw=password, owner_pw=owner, permissions=perm)


@tool(
    name="unlock", title="Unlock PDF", category="PDF Security",
    help="Remove PDF password security, giving you the freedom to use your PDFs as you want.",
    aliases=("decrypt",), requires=("pikepdf",),
    opts=[Opt("password", "str", None, "The document password (not needed for files that only restrict printing/copying).")],
)
def unlock(inputs, output: Path, password=None):
    try:
        import pikepdf

        try:
            with pikepdf.open(inputs[0], password=password or "") as pdf:
                pdf.save(output)  # saving without `encryption=` removes it
            return [output]
        except pikepdf.PasswordError:
            raise ToolError("wrong or missing password – use --password") from None
    except ImportError:
        doc = fitz.open(inputs[0])
        if doc.needs_pass and not doc.authenticate(password or ""):
            raise ToolError("wrong or missing password – use --password") from None
        return save_pdf(doc, output, encryption=fitz.PDF_ENCRYPT_NONE)


# ------------------------------------------------------------------ sign
@tool(
    name="sign", title="Sign PDF", category="PDF Security",
    help="Sign yourself: type or draw your signature (image), add initials and date – "
         "optionally with a real cryptographic certificate signature.",
    requires=("pyhanko",),
    opts=[
        Opt("name", "str", None, "Type your signature (rendered in a handwriting-style font)."),
        Opt("image", "file", None, "Signature image (white background is removed automatically)."),
        Opt("signature_font", "file", None, "A .ttf handwriting font for typed signatures."),
        Opt("page", "str", "last", "Page to sign."),
        Opt("position", "choice", "bottom-right", "Where to place the signature.", POSITIONS),
        Opt("x", "float", None, "Exact x position in points (overrides --position)."),
        Opt("y", "float", None, "Exact y position in points from the top (overrides --position)."),
        Opt("width", "float", 160, "Signature width in points."),
        Opt("color", "str", "#1a2b6d", "Ink colour for typed signatures."),
        Opt("date", "bool", True, "Write the date under the signature."),
        Opt("initials", "str", None, "Initials to stamp on every other page (bottom-right)."),
        Opt("cert", "file", None, "PKCS#12 (.p12/.pfx) certificate for a cryptographic digital signature (needs pyhanko)."),
        Opt("cert_password", "str", None, "Password of the certificate file."),
        Opt("reason", "str", None, "Signing reason (digital signature)."),
        Opt("location", "str", None, "Signing location (digital signature)."),
    ],
)
def sign(inputs, output: Path, name=None, image=None, signature_font=None, page="last", position="bottom-right",
         x=None, y=None, width=160, color="#1a2b6d", date=True, initials=None, cert=None, cert_password=None,
         reason=None, location=None):
    if not (name or image or cert):
        raise ToolError("give --name (typed signature), --image or --cert")
    doc = open_pdf(inputs[0])
    pno = parse_pages(page, doc.page_count)[0]
    pg = doc[pno]
    ink = parse_color(color)
    box = None
    if name or image:
        if image:
            data, (iw, ih) = _clean_signature(image)
            height = width * ih / iw
        else:
            font = _signature_font(signature_font)
            size = 34.0
            text_w = font.text_length(name, fontsize=size)
            size = min(size, size * width / max(text_w, 1))
            height = size * 1.3
        extra = 14 if date else 0
        if x is not None and y is not None:
            box = fitz.Rect(x, y, x + width, y + height + extra)
        else:
            box = place(pg.rect, width, height + extra, position, 36)
        sig_rect = fitz.Rect(box.x0, box.y0, box.x1, box.y0 + height)
        if image:
            pg.insert_image(sig_rect * pg.derotation_matrix, stream=data, rotate=pg.rotation)
        else:
            tw = fitz.TextWriter(pg.rect, color=ink)
            start = fitz.Point(sig_rect.x0, sig_rect.y1 - size * 0.3)
            tw.append(start * pg.derotation_matrix, name, font=font, fontsize=size)
            tw.write_text(pg, morph=(start * pg.derotation_matrix, fitz.Matrix(pg.rotation)) if pg.rotation else None)
        # signature line + date
        pg.draw_line(fitz.Point(box.x0, sig_rect.y1) * pg.derotation_matrix, fitz.Point(box.x1, sig_rect.y1) * pg.derotation_matrix,
                     color=(0.55, 0.55, 0.6), width=0.6)
        if date:
            stamp = dt.date.today().isoformat()
            pg.insert_text(fitz.Point(box.x0, box.y1 - 2) * pg.derotation_matrix, f"Signed {stamp}", fontsize=8,
                           color=(0.35, 0.35, 0.4), rotate=pg.rotation)
    if initials:
        font = _signature_font(signature_font)
        for i, other in enumerate(doc):
            if i == pno:
                continue
            w = font.text_length(initials, fontsize=18)
            r = place(other.rect, w, 18, "bottom-right", 24)
            tw = fitz.TextWriter(other.rect, color=ink)
            pt = fitz.Point(r.x0, r.y1) * other.derotation_matrix
            tw.append(pt, initials, font=font, fontsize=18)
            tw.write_text(other, morph=(pt, fitz.Matrix(other.rotation)) if other.rotation else None)
    if not cert:
        return save_pdf(doc, output)
    # cryptographic signature on top of the visual one
    buf = io.BytesIO(doc.tobytes(garbage=3, deflate=True))
    doc.close()
    _digital_sign(buf, output, cert, cert_password, pno, box, reason, location, name)
    return [output]


def _signature_font(path=None) -> fitz.Font:
    if path:
        return fitz.Font(fontfile=str(path))
    import glob

    # common handwriting / script fonts on Linux distributions
    for pattern in ("/usr/share/fonts/**/*Caveat*.ttf", "/usr/share/fonts/**/*DancingScript*.ttf",
                    "/usr/share/fonts/**/*GreatVibes*.ttf", "/usr/share/fonts/**/*Pacifico*.ttf",
                    "/usr/share/fonts/**/z003*.otf", "/usr/share/fonts/**/URWChancery*.otf",
                    "/usr/share/fonts/**/*Chancery*.t*f", "/usr/share/fonts/**/*Script*.ttf"):
        hits = glob.glob(pattern, recursive=True)
        if hits:
            return fitz.Font(fontfile=hits[0])
    return fitz.Font("tiit")  # Times Italic – always available


def _clean_signature(path):
    """Make a scanned/photographed signature transparent: near-white pixels become see-through."""
    from PIL import Image, ImageChops, ImageOps

    img = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    ink = img.convert("L").point(lambda v: 0 if v > 200 else min(255, (220 - v) * 3))
    img.putalpha(ImageChops.darker(ink, img.getchannel("A")))
    bbox = img.getchannel("A").getbbox()
    if bbox:
        img = img.crop(bbox)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), img.size


def _digital_sign(buf, output, cert, cert_password, pno, box, reason, location, name):
    try:
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        from pyhanko.sign import fields, signers
    except ImportError:
        raise ToolError("digital signatures need pyHanko: pip install 'pyhanko[image-support]'") from None
    signer = signers.SimpleSigner.load_pkcs12(cert, passphrase=cert_password.encode() if cert_password else None)
    if signer is None:
        raise ToolError("could not load the certificate (wrong password?)")
    w = IncrementalPdfFileWriter(buf)
    field_name = "Signature1"
    if box is not None:
        # pyHanko uses PDF coordinates (origin bottom-left)
        h = fitz.open("pdf", buf.getvalue())[pno].mediabox.height
        rect = (box.x0, h - box.y1, box.x1, h - box.y0)
        fields.append_signature_field(w, fields.SigFieldSpec(field_name, on_page=pno, box=rect))
    meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason, location=location, name=name)
    with open(output, "wb") as fh:
        signers.sign_pdf(w, meta, signer=signer, output=fh, existing_fields_only=box is not None)


# ------------------------------------------------------------------ redact
PRESETS = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "phone": r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?){2,4}\d{2,4}(?!\w)",
    "url": r"https?://\S+|www\.\S+",
    "ipv4": r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    "credit-card": r"\b(?:\d[ -]?){13,19}\b",
    "iban": r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "date": r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b",
}


@tool(
    name="redact", title="Redact PDF", category="PDF Security",
    help="Permanently remove sensitive text and graphics. Search terms, regex, presets (emails, phones, IBAN…) or areas.",
    opts=[
        Opt("text", "str", None, "Comma-separated words/phrases to black out."),
        Opt("regex", "str", None, "Regular expression to black out."),
        Opt("presets", "str", None, f"Comma list of: {', '.join(PRESETS)}."),
        Opt("areas", "str", None, "Rectangles 'page:x0,y0,x1,y1' separated by ';' (points from top-left)."),
        Opt("pages", "str", "all", "Pages to process."),
        Opt("fill", "str", "#000000", "Box colour."),
        Opt("label", "str", None, "Optional text written on each box, e.g. 'REDACTED'."),
        Opt("metadata", "bool", True, "Also wipe metadata, hidden text, comments and attachments."),
    ],
)
def redact(inputs, output: Path, text=None, regex=None, presets=None, areas=None, pages="all", fill="#000000",
           label=None, metadata=True):
    if not (text or regex or presets or areas):
        raise ToolError("give at least one of --text, --regex, --presets or --areas")
    doc = open_pdf(inputs[0])
    patterns = [re.compile(regex)] if regex else []
    for p in (presets or "").split(","):
        if p.strip():
            if p.strip() not in PRESETS:
                raise ToolError(f"unknown preset {p}; choose from {', '.join(PRESETS)}")
            patterns.append(re.compile(PRESETS[p.strip()]))
    terms = [t.strip() for t in (text or "").split(",") if t.strip()]
    rect_map: dict[int, list[fitz.Rect]] = {}
    for spec in (areas or "").split(";"):
        if spec.strip():
            pg, _, coords = spec.partition(":")
            for i in parse_pages(pg, doc.page_count):
                rect_map.setdefault(i, []).append(fitz.Rect([float(v) for v in coords.split(",")]))
    col = parse_color(fill)
    total = 0
    for pno in parse_pages(pages, doc.page_count):
        page = doc[pno]
        page_text = page.get_text()
        found = set(terms)
        for pat in patterns:
            found |= {m.group(0).strip() for m in pat.finditer(page_text) if m.group(0).strip()}
        targets = [q.rect for s in found for q in page.search_for(s, quads=True)]
        targets += rect_map.get(pno, [])
        for r in targets:
            page.add_redact_annot(r, text=label, fill=col, text_color=(1, 1, 1) if sum(col) < 1.5 else (0, 0, 0),
                                  fontsize=8)
        total += len(targets)
        if targets:
            page.apply_redactions()
    if metadata:
        doc.scrub(metadata=True, xml_metadata=True, attached_files=True, embedded_files=True, javascript=True,
                  hidden_text=True, thumbnails=True, redactions=False, reset_fields=False, reset_responses=True,
                  remove_links=False, clean_pages=True)
        for page in doc:
            for annot in list(page.annots() or []):
                page.delete_annot(annot)
    print(f"{total} area(s) redacted")
    return save_pdf(doc, output, garbage=4)


# ------------------------------------------------------------------ compare
@tool(
    name="compare", title="Compare PDF", category="PDF Security",
    help="Show a side-by-side document comparison and easily spot changes between two PDF versions.",
    multiple=True, min_inputs=2,
    opts=[
        Opt("mode", "choice", "both", "Highlight text changes, visual (pixel) changes, or both.", ["both", "text", "visual"]),
        Opt("dpi", "int", 100, "Resolution for the visual comparison."),
    ],
)
def compare(inputs, output: Path, mode="both", dpi=100):
    a, b = open_pdf(inputs[0]), open_pdf(inputs[1])
    out = fitz.open()
    changes = 0
    report = []
    for i in range(max(a.page_count, b.page_count)):
        pa = a[i] if i < a.page_count else None
        pb = b[i] if i < b.page_count else None
        ra = pa.rect if pa else pb.rect
        rb = pb.rect if pb else pa.rect
        gap = 20
        sheet = out.new_page(width=ra.width + rb.width + gap * 3, height=max(ra.height, rb.height) + 60)
        left = fitz.Rect(gap, 50, gap + ra.width, 50 + ra.height)
        right = fitz.Rect(gap * 2 + ra.width, 50, gap * 2 + ra.width + rb.width, 50 + rb.height)
        sheet.insert_text((gap, 30), f"{Path(inputs[0]).name}  –  page {i + 1}", fontsize=11)
        sheet.insert_text((right.x0, 30), f"{Path(inputs[1]).name}  –  page {i + 1}", fontsize=11)
        for r, p, d in ((left, pa, a), (right, pb, b)):
            if p is not None:
                sheet.show_pdf_page(r, d, p.number)
                sheet.draw_rect(r, color=(0.7, 0.7, 0.7), width=0.5)
            else:
                sheet.insert_text((r.x0 + 20, r.y0 + 40), "(page missing)", fontsize=14, color=(0.8, 0, 0))
        if not (pa and pb):
            changes += 1
            report.append(f"page {i + 1}: only in {'first' if pa else 'second'} file")
            continue
        if mode in ("both", "text"):
            n = _text_diff(pa, pb, sheet, left, right, report, i)
            changes += n
        if mode in ("both", "visual"):
            changes += _visual_diff(pa, pb, sheet, left, right, dpi)
    summary = f"{changes} difference(s) found" if changes else "the documents are identical"
    print(summary)
    for line in report[:200]:
        print("  " + line)
    first = out[0]
    first.insert_text((first.rect.width / 2 - 80, first.rect.height - 15), summary, fontsize=10, color=(0.8, 0.1, 0.1))
    return save_pdf(out, output)


def _text_diff(pa, pb, sheet, left, right, report, i):
    wa = pa.get_text("words", sort=True)
    wb = pb.get_text("words", sort=True)
    sm = difflib.SequenceMatcher(a=[w[4] for w in wa], b=[w[4] for w in wb], autojunk=False)
    n = 0
    sx, sy = left.width / pa.rect.width, left.height / pa.rect.height
    tx, ty = right.width / pb.rect.width, right.height / pb.rect.height
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            continue
        n += 1
        for w in wa[a0:a1]:  # removed / changed in A – red
            r = fitz.Rect(left.x0 + w[0] * sx, left.y0 + w[1] * sy, left.x0 + w[2] * sx, left.y0 + w[3] * sy)
            sheet.draw_rect(r, color=None, fill=(1, 0.2, 0.2), fill_opacity=0.35)
        for w in wb[b0:b1]:  # added / changed in B – green
            r = fitz.Rect(right.x0 + w[0] * tx, right.y0 + w[1] * ty, right.x0 + w[2] * tx, right.y0 + w[3] * ty)
            sheet.draw_rect(r, color=None, fill=(0.1, 0.8, 0.3), fill_opacity=0.35)
        old = " ".join(w[4] for w in wa[a0:a1])
        new = " ".join(w[4] for w in wb[b0:b1])
        report.append(f"page {i + 1}: {tag}: {old!r} → {new!r}"[:300])
    return n


def _visual_diff(pa, pb, sheet, left, right, dpi):
    """Outline (in orange, on the right page) the grid cells whose rendering differs."""
    from PIL import Image, ImageChops

    def render(page):
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        return Image.frombytes("L", (pix.width, pix.height), pix.samples)

    img_a, img_b = render(pa), render(pb)
    if img_a.size != img_b.size:
        img_b = img_b.resize(img_a.size)
    mask = ImageChops.difference(img_a, img_b).point(lambda v: 255 if v > 40 else 0)
    if mask.getbbox() is None:
        return 0
    w, h = mask.size
    cell = max(8, dpi // 6)
    sx, sy = right.width / w, right.height / h
    for cy in range(0, h, cell):
        for cx in range(0, w, cell):
            if mask.crop((cx, cy, cx + cell, cy + cell)).getbbox():
                r = fitz.Rect(right.x0 + cx * sx, right.y0 + cy * sy, right.x0 + (cx + cell) * sx, right.y0 + (cy + cell) * sy)
                sheet.draw_rect(r, color=(1, 0.5, 0), width=0.8)
    return 1
