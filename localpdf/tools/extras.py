"""Bonus tools that iLovePDF doesn't offer."""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import ToolError, ensure_dir, ghostscript, human_size, load_json_arg, open_pdf, run, save_pdf


@tool(
    name="info", title="PDF info", category="Extras", extra=True, output="none",
    help="Show pages, sizes, fonts, images, encryption, forms, metadata and whether the PDF is scanned.",
    opts=[Opt("json", "bool", False, "Print machine-readable JSON.")],
)
def info(inputs, output, json_=False, **kw):
    as_json = kw.get("json", json_)
    p = Path(inputs[0])
    doc = fitz.open(p)
    data = {"file": p.name, "size": human_size(p.stat().st_size), "encrypted": doc.is_encrypted,
            "needs_password": doc.needs_pass}
    if doc.needs_pass:
        return json.dumps(data, indent=2) if as_json else _fmt(data)
    sizes = {}
    fonts, images, text_pages = set(), 0, 0
    for page in doc:
        r = page.rect
        key = f"{r.width / 72 * 25.4:.0f}×{r.height / 72 * 25.4:.0f} mm"
        sizes[key] = sizes.get(key, 0) + 1
        fonts |= {f[3] for f in page.get_fonts()}
        images += len(page.get_images())
        text_pages += bool(page.get_text().strip())
    data.update({
        "pages": doc.page_count,
        "pdf_version": doc.metadata.get("format"),
        "page_sizes": sizes,
        "metadata": {k: v for k, v in doc.metadata.items() if v and k not in ("format", "encryption")},
        "fonts": sorted(fonts),
        "images": images,
        "pages_with_text": text_pages,
        "looks_scanned": text_pages == 0 and images > 0,
        "bookmarks": len(doc.get_toc()),
        "form_fields": sum(1 for page in doc for _ in page.widgets()),
        "attachments": doc.embfile_names(),
        "permissions": doc.permissions,
    })
    if doc.is_encrypted or doc.metadata.get("encryption"):
        data["encryption"] = doc.metadata.get("encryption")
    return json.dumps(data, indent=2, ensure_ascii=False) if as_json else _fmt(data)


def _fmt(data: dict) -> str:
    lines = []
    for k, v in data.items():
        if isinstance(v, dict):
            lines.append(f"{k.replace('_', ' ')}:")
            lines += [f"    {kk}: {vv}" for kk, vv in v.items()]
        elif isinstance(v, list):
            lines.append(f"{k.replace('_', ' ')}: {', '.join(map(str, v)) if v else '-'}")
        else:
            lines.append(f"{k.replace('_', ' ')}: {v}")
    return "\n".join(lines)


@tool(
    name="metadata", title="Edit metadata", category="Extras", extra=True,
    help="Change title, author, subject and keywords – or wipe all metadata for privacy.",
    opts=[
        Opt("title", "str", None, "Document title."),
        Opt("author", "str", None, "Author."),
        Opt("subject", "str", None, "Subject."),
        Opt("keywords", "str", None, "Keywords."),
        Opt("creator", "str", None, "Creating application."),
        Opt("clear", "bool", False, "Remove ALL metadata (including XMP) first."),
    ],
)
def metadata(inputs, output: Path, title=None, author=None, subject=None, keywords=None, creator=None, clear=False):
    doc = open_pdf(inputs[0])
    meta = {k: v for k, v in doc.metadata.items() if k not in ("format", "encryption")}
    if clear:
        doc.del_xml_metadata()
        meta = {k: "" for k in meta}
    for k, v in (("title", title), ("author", author), ("subject", subject), ("keywords", keywords), ("creator", creator)):
        if v is not None:
            meta[k] = v
    doc.set_metadata(meta)
    return save_pdf(doc, output)


@tool(
    name="flatten", title="Flatten PDF", category="Extras", extra=True,
    help="Burn form fields and annotations into the page so they can't be changed (or rasterize the whole PDF).",
    opts=[Opt("rasterize", "bool", False, "Convert every page into an image (nothing selectable/editable remains)."),
          Opt("dpi", "int", 200, "Resolution when rasterizing.")],
)
def flatten(inputs, output: Path, rasterize=False, dpi=200):
    doc = open_pdf(inputs[0])
    if rasterize:
        out = fitz.open()
        for page in doc:
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=dpi))
        return save_pdf(out, output)
    if hasattr(doc, "bake"):
        doc.bake(annots=True, widgets=True)
    else:  # older PyMuPDF: round-trip through a "print" of each page
        out = fitz.open()
        for page in doc:
            out.new_page(width=page.rect.width, height=page.rect.height).show_pdf_page(page.rect, doc, page.number)
        doc = out
    return save_pdf(doc, output)


@tool(
    name="grayscale", title="Grayscale PDF", category="Extras", extra=True,
    help="Convert all colours to shades of gray – save ink when printing.", requires=("gs",),
    opts=[Opt("dpi", "int", 200, "Resolution of the fallback (rasterizing) engine when Ghostscript is missing.")],
)
def grayscale(inputs, output: Path, dpi=200):
    gs = ghostscript()
    if gs:
        run([gs, "-sDEVICE=pdfwrite", "-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray",
             "-dOverrideICC", "-dNOPAUSE", "-dBATCH", "-dQUIET", f"-sOutputFile={output}", inputs[0]])
        return [output]
    doc = open_pdf(inputs[0])
    out = fitz.open()
    for page in doc:
        new = out.new_page(width=page.rect.width, height=page.rect.height)
        new.insert_image(new.rect, pixmap=page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY))
    print("(Ghostscript not installed – pages were rasterized)")
    return save_pdf(out, output)


@tool(
    name="fill-form", title="Fill PDF form", category="Extras", extra=True,
    help="List the fields of a fillable PDF form, or fill them from JSON (and optionally flatten).",
    opts=[
        Opt("data", "text", None, 'JSON object {"field name": "value", "checkbox": true} or path to a .json file. '
                                  "Omit to list the fields."),
        Opt("flatten", "bool", False, "Make the filled form non-editable."),
    ],
)
def fill_form(inputs, output: Path, data=None, flatten=False):
    doc = open_pdf(inputs[0])
    widgets = [(page.number, w) for page in doc for w in page.widgets()]
    if not widgets:
        raise ToolError("this PDF has no form fields")
    if not data:
        listing = {w.field_name: {"type": w.field_type_string, "value": w.field_value, "page": pno + 1,
                                  **({"options": w.choice_values} if w.choice_values else {})} for pno, w in widgets}
        return json.dumps(listing, indent=2, ensure_ascii=False)
    values = load_json_arg(data)
    missing = set(values) - {w.field_name for _, w in widgets}
    if missing:
        raise ToolError(f"unknown field(s): {', '.join(sorted(missing))}")
    for page in doc:
        for w in page.widgets():
            if w.field_name in values:
                v = values[w.field_name]
                if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
                    w.field_value = w.on_state() if v in (True, "true", "yes", "on", 1, "1") else "Off"
                else:
                    w.field_value = str(v)
                w.update()
    if flatten and hasattr(doc, "bake"):
        doc.bake(annots=True, widgets=True)
    return save_pdf(doc, output)


@tool(
    name="attachments", title="PDF attachments", category="Extras", extra=True, accept=(), multiple=True,
    help="Embed files inside a PDF (first input = the PDF), or extract every embedded file (single input).",
    output="dir",
)
def attachments(inputs, output: Path):
    doc = open_pdf(inputs[0])
    if len(inputs) == 1:
        names = doc.embfile_names()
        if not names:
            raise ToolError("no embedded files in this PDF")
        ensure_dir(output)
        written = []
        for name in names:
            target = output / Path(name).name
            target.write_bytes(doc.embfile_get(name))
            written.append(target)
        return written
    for f in inputs[1:]:
        doc.embfile_add(Path(f).name, Path(f).read_bytes(), filename=Path(f).name)
    ensure_dir(output)
    return save_pdf(doc, output / Path(inputs[0]).name)


@tool(
    name="bookmarks", title="Edit bookmarks", category="Extras", extra=True,
    help="Export the outline (table of contents) as JSON, replace it from JSON, or auto-generate it from headings.",
    opts=[
        Opt("set", "text", None, 'JSON list [[level, "Title", page], …] or a .json file. Omit to print the current outline.'),
        Opt("auto", "bool", False, "Generate bookmarks from large/bold headings."),
    ],
)
def bookmarks(inputs, output: Path, auto=False, **kw):
    toc_json = kw.get("set")
    doc = open_pdf(inputs[0])
    if not toc_json and not auto:
        return json.dumps(doc.get_toc(), indent=2, ensure_ascii=False)
    if auto:
        toc = _auto_toc(doc)
        if not toc:
            raise ToolError("no headings detected")
    else:
        toc = load_json_arg(toc_json)
    doc.set_toc(toc)
    return save_pdf(doc, output)


def _auto_toc(doc):
    from collections import Counter

    sizes = Counter()
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                for s in line["spans"]:
                    sizes[round(s["size"], 1)] += len(s["text"])
    body = sizes.most_common(1)[0][0]
    heading_sizes = sorted({s for s in sizes if s >= body * 1.15}, reverse=True)[:3]
    toc = []
    for page in doc:
        for b in page.get_text("dict")["blocks"]:
            for line in b.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                size = max((round(s["size"], 1) for s in line["spans"]), default=0)
                if text and size in heading_sizes and len(text) < 150:
                    toc.append([heading_sizes.index(size) + 1, text, page.number + 1])
    # levels must start at 1 and not jump
    fixed, prev = [], 0
    for lvl, title, pno in toc:
        lvl = min(lvl, prev + 1)
        fixed.append([lvl, title, pno])
        prev = lvl
    return fixed
