"""Edit PDF: rotate, page numbers, watermark, crop, edit (text/images/shapes/replace)."""
from __future__ import annotations

import io
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import POSITIONS, ToolError, load_json_arg, open_pdf, parse_pages, place, save_pdf
from ..utils import color as parse_color

FONTS = {"helvetica": "helv", "times": "tiro", "courier": "cour", "helvetica-bold": "hebo",
         "times-bold": "tibo", "courier-bold": "cobo", "times-italic": "tiit"}


def _font(name: str | None) -> fitz.Font:
    """Built-in base-14 name or path to a .ttf/.otf file."""
    name = name or "helvetica"
    if Path(name).suffix.lower() in (".ttf", ".otf") and Path(name).exists():
        return fitz.Font(fontfile=name)
    if name.lower() not in FONTS:
        raise ToolError(f"font must be a .ttf/.otf path or one of {', '.join(FONTS)}")
    return fitz.Font(FONTS[name.lower()])


@tool(
    name="rotate", title="Rotate PDF", category="Edit PDF",
    help="Rotate your PDFs the way you need them. You can even rotate multiple PDFs at once!",
    opts=[
        Opt("angle", "choice", "90", "Clockwise rotation in degrees.", ["90", "180", "270", "-90"]),
        Opt("pages", "str", "all", "Pages to rotate, e.g. 'even' or '1,3-4'."),
    ],
)
def rotate(inputs, output: Path, angle="90", pages="all"):
    doc = open_pdf(inputs[0])
    for i in parse_pages(pages, doc.page_count):
        doc[i].set_rotation((doc[i].rotation + int(angle)) % 360)
    return save_pdf(doc, output)


@tool(
    name="page-numbers", title="Add page numbers", category="Edit PDF",
    help="Add page numbers into PDFs with ease. Choose your positions, dimensions, typography.",
    aliases=("number",),
    opts=[
        Opt("position", "choice", "bottom-center", "Where to place the number.", POSITIONS),
        Opt("format", "str", "{n}", "Text template: {n} page number, {total} page count. E.g. 'Page {n} of {total}'."),
        Opt("start", "int", 1, "Number given to the first numbered page."),
        Opt("pages", "str", "all", "Pages to number, e.g. '2-' to skip the cover."),
        Opt("font_size", "float", 11, "Font size."),
        Opt("font", "str", "helvetica", "helvetica, times, courier (+ -bold) or a .ttf path."),
        Opt("color", "str", "#000000", "Text colour."),
        Opt("margin", "float", 28, "Distance from the page edge in points."),
        Opt("facing", "bool", False, "Facing pages: mirror left/right positions on even pages (books)."),
    ],
)
def page_numbers(inputs, output: Path, position="bottom-center", format="{n}", start=1, pages="all", font_size=11,
                 font="helvetica", color="#000000", margin=28, facing=False):
    col = parse_color(color)
    doc = open_pdf(inputs[0])
    fnt = _font(font)
    targets = parse_pages(pages, doc.page_count)
    total = len(targets) + start - 1
    for k, pno in enumerate(targets):
        page = doc[pno]
        text = format.format(n=start + k, total=total, page=pno + 1, pages=doc.page_count)
        pos = position
        if facing and (pno + 1) % 2 == 0:
            pos = pos.replace("left", "@").replace("right", "left").replace("@", "right")
        w = fnt.text_length(text, fontsize=font_size)
        rect = place(page.rect, w, font_size, pos, margin)
        _write(page, fitz.Point(rect.x0, rect.y1 - font_size * 0.2), text, fnt, font_size, col)
    return save_pdf(doc, output)


def _write(page, point, text, fnt, size, col, opacity=1.0, angle=0, overlay=True):
    """Write text so it appears upright regardless of the page's /Rotate value."""
    tw = fitz.TextWriter(page.rect, opacity=opacity, color=col)
    point = point * page.derotation_matrix
    tw.append(point, text, font=fnt, fontsize=size)
    matrix = fitz.Matrix(page.rotation + angle) if (page.rotation or angle) else None
    tw.write_text(page, morph=(point, matrix) if matrix else None, overlay=overlay)


@tool(
    name="watermark", title="Add watermark", category="Edit PDF",
    help="Stamp an image or text over your PDF in seconds. Choose typography, transparency and position.",
    opts=[
        Opt("text", "str", None, "Watermark text."),
        Opt("image", "file", None, "Watermark image (PNG with transparency works best)."),
        Opt("position", "choice", "center", "Placement.", POSITIONS + ["tile"]),
        Opt("opacity", "float", 0.3, "0 = invisible, 1 = opaque."),
        Opt("rotation", "float", 45, "Rotation in degrees (counter-clockwise)."),
        Opt("font_size", "float", 60, "Text size."),
        Opt("font", "str", "helvetica-bold", "helvetica, times, courier (+ -bold) or a .ttf path."),
        Opt("color", "str", "#e5322d", "Text colour."),
        Opt("scale", "float", 0.4, "Image width as a fraction of the page width."),
        Opt("pages", "str", "all", "Pages to stamp."),
        Opt("layer", "choice", "over", "Over or under the page content.", ["over", "under"]),
    ],
)
def watermark(inputs, output: Path, text=None, image=None, position="center", opacity=0.3, rotation=45, font_size=60,
              font="helvetica-bold", color="#e5322d", scale=0.4, pages="all", layer="over"):
    if not text and not image:
        raise ToolError("give --text or --image")
    doc = open_pdf(inputs[0])
    overlay = layer == "over"
    fnt = _font(font)
    col = parse_color(color)
    img_bytes = _prepared_image(image, opacity, rotation) if image else None
    for pno in parse_pages(pages, doc.page_count):
        page = doc[pno]
        r = page.rect
        if text:
            w = fnt.text_length(text, fontsize=font_size)
            spots = _tile_points(r, w, font_size) if position == "tile" else [place(r, w, font_size, position, 36)]
            for spot in spots:
                # lay the text out horizontally around its centre (in unrotated page space),
                # then let `morph` turn it upright for the viewer plus the requested angle
                pivot = fitz.Point((spot.x0 + spot.x1) / 2, (spot.y0 + spot.y1) / 2) * page.derotation_matrix
                start = fitz.Point(pivot.x - w / 2, pivot.y + font_size * 0.35)
                tw = fitz.TextWriter(page.rect, opacity=opacity, color=col)
                tw.append(start, text, font=fnt, fontsize=font_size)
                tw.write_text(page, morph=(pivot, fitz.Matrix(page.rotation + rotation)), overlay=overlay)
        if img_bytes:
            iw, ih = img_bytes[1]
            w = r.width * scale
            h = w * ih / iw
            spots = _tile_points(r, w, h) if position == "tile" else [place(r, w, h, position, 36)]
            for spot in spots:
                page.insert_image(spot * page.derotation_matrix, stream=img_bytes[0], overlay=overlay,
                                  rotate=page.rotation)
    return save_pdf(doc, output)


def _tile_points(r, w, h):
    gap_x, gap_y = w * 0.6 + 40, h * 3 + 60
    out = []
    y = r.y0 + 20
    row = 0
    while y < r.y1:
        x = r.x0 - (w / 2 if row % 2 else 0)
        while x < r.x1:
            out.append(fitz.Rect(x, y, x + w, y + h))
            x += w + gap_x
        y += gap_y
        row += 1
    return out


def _prepared_image(path, opacity, rotation):
    """Apply opacity and arbitrary rotation with Pillow (PDF images can only rotate by 90°)."""
    from PIL import Image

    img = Image.open(path).convert("RGBA")
    if rotation:
        img = img.rotate(rotation, expand=True, resample=Image.BICUBIC)
    if opacity < 1:
        alpha = img.getchannel("A").point(lambda a: int(a * opacity))
        img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), img.size


@tool(
    name="crop", title="Crop PDF", category="Edit PDF",
    help="Crop margins of PDF documents or select specific areas – or trim the white space automatically.",
    opts=[
        Opt("auto", "bool", False, "Trim to the visible content of each page automatically."),
        Opt("margins", "str", None, "Amount to cut: 'all' or 'top,right,bottom,left' (in --unit)."),
        Opt("box", "str", None, "Keep only this area: 'x0,y0,x1,y1' measured from the top-left (in --unit)."),
        Opt("unit", "choice", "mm", "Unit for --margins / --box.", ["mm", "pt", "in", "percent"]),
        Opt("padding", "float", 5, "Auto mode: keep this many mm around the content."),
        Opt("pages", "str", "all", "Pages to crop."),
    ],
)
def crop(inputs, output: Path, auto=False, margins=None, box=None, unit="mm", padding=5, pages="all"):
    if not (auto or margins or box):
        raise ToolError("use --auto, --margins or --box")
    factor = {"mm": 72 / 25.4, "pt": 1, "in": 72}

    def conv(v, axis_len):
        return float(v) / 100 * axis_len if unit == "percent" else float(v) * factor[unit]

    doc = open_pdf(inputs[0])
    for pno in parse_pages(pages, doc.page_count):
        page = doc[pno]
        page.set_cropbox(page.mediabox)  # start from the full page
        r = page.rect if page.rotation == 0 else page.mediabox

        if auto:
            content = _content_bbox(page)
            if content is None:
                continue
            pad = padding * 72 / 25.4
            new = (content + (-pad, -pad, pad, pad)) & page.mediabox
        elif box:
            x0, y0, x1, y1 = [float(v) for v in box.split(",")]
            new = fitz.Rect(conv(x0, r.width), conv(y0, r.height), conv(x1, r.width), conv(y1, r.height))
        else:
            vals = [v for v in margins.split(",")]
            if len(vals) == 1:
                vals *= 4
            if len(vals) != 4:
                raise ToolError("--margins needs 1 or 4 values: top,right,bottom,left")
            t, rt, b, lf = vals
            new = fitz.Rect(conv(lf, r.width), conv(t, r.height), r.width - conv(rt, r.width), r.height - conv(b, r.height))
        if new.is_empty or new.width < 10 or new.height < 10:
            raise ToolError(f"crop area on page {pno + 1} is empty")
        # cropbox coordinates are un-rotated, top-left based like page coordinates in PyMuPDF
        page.set_cropbox(new * page.derotation_matrix if auto else new)
    return save_pdf(doc, output)


def _content_bbox(page) -> fitz.Rect | None:
    rect = fitz.Rect()
    for _kind, bbox in [(b[0], b[1]) for b in page.get_bboxlog()]:
        rb = fitz.Rect(bbox)
        if rb.is_valid and not rb.is_empty and rb.width < page.rect.width * 0.999:
            rect |= rb
    if rect.is_empty:
        return None
    # bboxlog is in page (rotated) coordinates – map back to the unrotated mediabox space
    return rect * page.derotation_matrix if page.rotation else rect


EDIT_HELP = """Edit operations, as a JSON list (file or inline). Coordinates are points from the top-left corner.
  {"type":"text","page":1,"x":72,"y":100,"text":"Hello","size":14,"color":"#1f5fd1","font":"helvetica"}
  {"type":"textbox","page":1,"rect":[72,100,300,200],"text":"wrapped paragraph…","size":11,"align":"left"}
  {"type":"image","page":1,"rect":[400,50,550,120],"file":"logo.png"}
  {"type":"rect","page":1,"rect":[50,50,200,90],"color":"#e5322d","fill":"#ffd400","width":1.5,"opacity":0.5}
  {"type":"ellipse"|"line","page":1,"rect"|"from"/"to":…}
  {"type":"highlight","page":"all","search":"important","color":"#ffd400"}
  {"type":"underline"|"strike","search":"text"}
  {"type":"whiteout","page":1,"rect":[50,50,200,90]}
  {"type":"replace","page":"all","search":"2023","with":"2024"}
  {"type":"note","page":1,"x":20,"y":20,"text":"sticky note"}
  {"type":"link","page":1,"rect":[72,700,200,720],"url":"https://example.com"}"""


@tool(
    name="edit", title="Edit PDF", category="Edit PDF",
    help="Add text, images, shapes, highlights, notes and links – or find & replace text – in a PDF.",
    opts=[
        Opt("ops", "text", None, EDIT_HELP),
        Opt("add_text", "str", None, "Quick mode: text to add (use with --x --y --page)."),
        Opt("add_image", "file", None, "Quick mode: image to add (use with --x --y --width)."),
        Opt("page", "int", 1, "Quick mode: page number."),
        Opt("x", "float", 72, "Quick mode: x position in points from the left."),
        Opt("y", "float", 72, "Quick mode: y position in points from the top."),
        Opt("size", "float", 12, "Quick mode: font size."),
        Opt("width", "float", 150, "Quick mode: image width in points."),
        Opt("color", "str", "#000000", "Quick mode: text colour."),
        Opt("find", "str", None, "Find this text…"),
        Opt("replace", "str", None, "…and replace it with this (keeps position, best effort font match)."),
    ],
)
def edit(inputs, output: Path, ops=None, add_text=None, add_image=None, page=1, x=72, y=72, size=12, width=150,
         color="#000000", find=None, replace=None):
    operations = []
    if ops:
        operations = load_json_arg(ops)
        if isinstance(operations, dict):
            operations = [operations]
    if add_text:
        operations.append({"type": "text", "page": page, "x": x, "y": y, "text": add_text, "size": size, "color": color})
    if add_image:
        from PIL import Image

        iw, ih = Image.open(add_image).size
        operations.append({"type": "image", "page": page, "file": add_image,
                           "rect": [x, y, x + width, y + width * ih / iw]})
    if find is not None and replace is not None:
        operations.append({"type": "replace", "page": "all", "search": find, "with": replace})
    if not operations:
        raise ToolError("nothing to do – give --ops, --add-text, --add-image or --find/--replace")
    doc = open_pdf(inputs[0])
    for op in operations:
        _apply(doc, op)
    return save_pdf(doc, output)


def _pages(doc, op):
    spec = op.get("page", 1)
    return parse_pages(str(spec), doc.page_count)


def _apply(doc, op):
    kind = op.get("type")
    for pno in _pages(doc, op):
        page = doc[pno]
        col = parse_color(op.get("color"), "#000000")
        if kind == "text":
            _write(page, fitz.Point(op["x"], op["y"]), op["text"], _font(op.get("font")), op.get("size", 12), col,
                   op.get("opacity", 1), op.get("rotate", 0))
        elif kind == "textbox":
            align = {"left": 0, "center": 1, "right": 2, "justify": 3}[op.get("align", "left")]
            css = f"* {{font-family: sans-serif; font-size: {op.get('size', 11)}pt; color: {op.get('color', '#000')}; text-align: " \
                  f"{['left', 'center', 'right', 'justify'][align]}; }}"
            from html import escape

            page.insert_htmlbox(fitz.Rect(op["rect"]), escape(op["text"]).replace("\n", "<br>"), css=css,
                                rotate=page.rotation)
        elif kind == "image":
            page.insert_image(fitz.Rect(op["rect"]) * page.derotation_matrix, filename=op["file"],
                              rotate=page.rotation)
        elif kind in ("rect", "ellipse", "whiteout"):
            r = fitz.Rect(op["rect"]) * page.derotation_matrix
            fill = (1, 1, 1) if kind == "whiteout" else (parse_color(op["fill"]) if op.get("fill") else None)
            stroke = None if kind == "whiteout" else col
            shape = page.new_shape()
            (shape.draw_oval if kind == "ellipse" else shape.draw_rect)(r)
            shape.finish(color=stroke, fill=fill, width=op.get("width", 1), fill_opacity=op.get("opacity", 1),
                         stroke_opacity=op.get("opacity", 1))
            shape.commit()
        elif kind == "line":
            page.draw_line(fitz.Point(op["from"]) * page.derotation_matrix, fitz.Point(op["to"]) * page.derotation_matrix,
                           color=col, width=op.get("width", 1))
        elif kind in ("highlight", "underline", "strike"):
            quads = page.search_for(op["search"], quads=True)
            if quads:
                add = {"highlight": page.add_highlight_annot, "underline": page.add_underline_annot,
                       "strike": page.add_strikeout_annot}[kind]
                annot = add(quads)
                annot.set_colors(stroke=parse_color(op.get("color"), "#ffd400" if kind == "highlight" else "#e5322d"))
                annot.update()
        elif kind == "note":
            page.add_text_annot(fitz.Point(op["x"], op["y"]) * page.derotation_matrix, op["text"])
        elif kind == "link":
            page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(op["rect"]), "uri": op["url"]})
        elif kind == "replace":
            _replace(page, op["search"], op["with"], op)
        else:
            raise ToolError(f"unknown edit operation type {kind!r}")


def _replace(page, old, new, op):
    """Redact every occurrence of `old`, then write `new` in its place using a similar font size/colour."""
    hits = page.search_for(old)
    if not hits:
        return
    spans = [s for b in page.get_text("dict")["blocks"] for line in b.get("lines", []) for s in line["spans"]]
    styles = []
    for rect in hits:
        span = next((s for s in spans if fitz.Rect(s["bbox"]).intersects(rect)), None)
        size = span["size"] if span else rect.height * 0.8
        srgb = span["color"] if span else 0
        col = ((srgb >> 16) / 255, ((srgb >> 8) & 255) / 255, (srgb & 255) / 255)
        bold = bool(span and ("Bold" in span["font"] or span["flags"] & 16))
        serif = bool(span and (span["flags"] & 4))
        fontname = ("tibo" if bold else "tiro") if serif else ("hebo" if bold else "helv")
        origin = fitz.Point(span["origin"]) if span else fitz.Point(rect.x0, rect.y1 - rect.height * 0.2)
        styles.append((rect, size, col, fontname, fitz.Point(rect.x0, origin.y)))
        page.add_redact_annot(rect, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
    for _rect, size, col, fontname, origin in styles:
        page.insert_text(origin, new, fontsize=op.get("size", size), fontname=fontname, color=col)
