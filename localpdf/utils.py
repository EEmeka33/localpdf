"""Shared helpers: page ranges, colours, external programs, file handling."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator

import pymupdf as fitz

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp", ".heic", ".heif", ".jxl", ".pnm", ".ppm")
WORD_EXTS = (".doc", ".docx", ".odt", ".rtf", ".wpd", ".dot", ".dotx", ".pages")
PPT_EXTS = (".ppt", ".pptx", ".odp", ".pps", ".ppsx", ".key")
EXCEL_EXTS = (".xls", ".xlsx", ".ods", ".csv", ".xlsm", ".numbers")
HTML_EXTS = (".html", ".htm", ".xhtml", ".mhtml", ".svg")

PAPER_SIZES = {
    "a3": (842, 1191),
    "a4": (595, 842),
    "a5": (420, 595),
    "letter": (612, 792),
    "legal": (612, 1008),
    "tabloid": (792, 1224),
}

POSITIONS = [
    "top-left", "top-center", "top-right",
    "middle-left", "center", "middle-right",
    "bottom-left", "bottom-center", "bottom-right",
]


class ToolError(Exception):
    """A user-facing error (bad option, missing dependency, ...)."""


# --------------------------------------------------------------------------- pages
def parse_pages(spec: str | None, count: int) -> list[int]:
    """Parse a 1-based page spec into 0-based indices (order preserved).

    Supports ``all``, ``odd``, ``even``, ``last`` (or ``end``), ranges like
    ``2-5``, open ranges ``3-`` / ``-4``, reversed ranges ``9-1`` and
    qpdf-style pages counted from the end (``r1`` = last, ``r2`` = second last).
    """
    if spec is None or str(spec).strip().lower() in ("", "all", "*"):
        return list(range(count))

    def num(s: str) -> int:
        if s in ("last", "end", "n", "z"):
            return count
        if s.startswith("r") and s[1:].isdigit():
            return count - int(s[1:]) + 1
        if s.isdigit():
            return int(s)
        raise ToolError(f"invalid page {s!r} in page list {spec!r}")

    out: list[int] = []
    for tok in str(spec).replace(" ", "").lower().split(","):
        if not tok:
            continue
        if tok in ("odd", "even"):
            out += list(range(0 if tok == "odd" else 1, count, 2))
            continue
        if "-" in tok:
            a, b = tok.split("-", 1)
            start, stop = (num(a) if a else 1), (num(b) if b else count)
            step = 1 if stop >= start else -1
            pages = range(start, stop + step, step)
        else:
            pages = [num(tok)]
        for p in pages:
            if not 1 <= p <= count:
                raise ToolError(f"page {p} out of range (document has {count} pages)")
            out.append(p - 1)
    return out


def parse_groups(spec: str, count: int) -> list[list[int]]:
    """``"1-3;4-6;7"`` → several page groups (``;`` or ``|`` separates groups)."""
    return [parse_pages(g, count) for g in re.split(r"[;|]", spec) if g.strip()]


# --------------------------------------------------------------------------- colours
NAMED = {
    "black": "#000000", "white": "#ffffff", "red": "#e5322d", "green": "#2e9e44",
    "blue": "#1f5fd1", "gray": "#808080", "grey": "#808080", "yellow": "#ffd400",
    "orange": "#ff8c00", "purple": "#7b3fbf",
}


def color(value: str | None, default: str = "#000000") -> tuple[float, float, float]:
    v = (value or default).strip().lower()
    v = NAMED.get(v, v)
    if re.fullmatch(r"#?[0-9a-f]{3}", v):
        v = "".join(c * 2 for c in v.lstrip("#"))
    v = v.lstrip("#")
    if not re.fullmatch(r"[0-9a-f]{6}", v):
        raise ToolError(f"invalid colour {value!r} – use a name or #rrggbb")
    return tuple(int(v[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


# --------------------------------------------------------------------------- geometry
def place(page_rect: fitz.Rect, w: float, h: float, position: str, margin: float) -> fitz.Rect:
    """Rect of size w×h placed at a named position inside page_rect."""
    if position not in POSITIONS:
        raise ToolError(f"position must be one of: {', '.join(POSITIONS)}")
    vert, horiz = ("middle", "center") if position == "center" else position.split("-")
    x = {"left": page_rect.x0 + margin, "center": (page_rect.x0 + page_rect.x1 - w) / 2, "right": page_rect.x1 - margin - w}[horiz]
    y = {"top": page_rect.y0 + margin, "middle": (page_rect.y0 + page_rect.y1 - h) / 2, "bottom": page_rect.y1 - margin - h}[vert]
    return fitz.Rect(x, y, x + w, y + h)


# --------------------------------------------------------------------------- files
def open_pdf(path: str | Path, password: str | None = None) -> fitz.Document:
    """Open a PDF (or anything PyMuPDF reads – images, XPS, EPUB…) as a PDF document."""
    p = Path(path)
    if not p.exists():
        raise ToolError(f"file not found: {p}")
    doc = fitz.open(p)
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            raise ToolError(f"{p.name} is password protected – pass --password")
    if not doc.is_pdf:
        pdf_bytes = doc.convert_to_pdf()
        doc = fitz.open("pdf", pdf_bytes)
    return doc


def save_pdf(doc: fitz.Document, output: Path, **kw) -> list[Path]:
    output.parent.mkdir(parents=True, exist_ok=True)
    opts = dict(garbage=3, deflate=True)
    opts.update(kw)
    doc.save(output, **opts)
    doc.close()
    return [output]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def tempdir() -> Iterator[Path]:
    d = tempfile.mkdtemp(prefix="localpdf-")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- external programs
def which(*names: str) -> str | None:
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def require(*names: str, hint: str = "") -> str:
    p = which(*names)
    if not p:
        msg = f"this tool needs `{names[0]}` but it is not installed."
        if hint:
            msg += f" {hint}"
        msg += " Run `localpdf doctor` for install instructions."
        raise ToolError(msg)
    return p


def run(cmd: Iterable[str | os.PathLike], timeout: int = 600, **kw) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as e:
        raise ToolError(f"{Path(cmd[0]).name} timed out after {timeout}s") from e
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip().splitlines()[-5:]
        raise ToolError(f"{Path(cmd[0]).name} failed: " + " | ".join(err))
    return res


def ghostscript() -> str | None:
    return which("gs", "gswin64c", "gsc")


def soffice_convert(src: Path, outdir: Path, fmt: str, infilter: str | None = None) -> Path:
    """Convert with LibreOffice in headless mode, returns the produced file."""
    exe = require("soffice", "libreoffice", hint="Install LibreOffice (apt install libreoffice).")
    with tempdir() as profile:
        cmd = [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore"]
        if infilter:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", fmt, "--outdir", str(outdir), str(src)]
        run(cmd, timeout=900)
    produced = outdir / (src.stem + "." + fmt.split(":")[0])
    if not produced.exists():
        raise ToolError(f"LibreOffice could not convert {src.name}")
    return produced


def load_json_arg(value: str):
    """Options like --ops/--data accept inline JSON or a path to a .json file."""
    import json

    text = value.strip()
    if not text.startswith(("[", "{")):
        p = Path(text)
        if not p.is_file():
            raise ToolError(f"{value!r} is neither JSON nor an existing .json file")
        text = p.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ToolError(f"invalid JSON: {e}") from None


def reading_order(page) -> list[str]:
    """Text blocks of a page in reading order (multi-column aware), using a recursive XY-cut.

    The region is split at a vertical gutter that no block crosses (columns, read left to right)
    and otherwise at a horizontal gap (bands, read top to bottom); unsplittable regions are read
    top-to-bottom. Content-stream order and naive y-sorting both fail on many multi-column PDFs.
    """
    blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]

    def cut(items, axis):
        lo, hi = (0, 2) if axis == "x" else (1, 3)
        items = sorted(items, key=lambda b: b[lo])
        groups, end = [[items[0]]], items[0][hi]
        for b in items[1:]:
            if b[lo] > end + 1:  # a gap nothing crosses
                groups.append([b])
            else:
                groups[-1].append(b)
            end = max(end, b[hi])
        return groups

    def order(items):
        if len(items) <= 1:
            return items
        for axis in ("x", "y"):
            groups = cut(items, axis)
            if len(groups) > 1:
                return [b for g in groups for b in order(g)]
        return sorted(items, key=lambda b: (b[1], b[0]))

    return [b[4] for b in order(blocks)]


def as_path_list(inputs: Iterable[str]) -> list[Path]:
    paths = [Path(i) for i in inputs]
    for p in paths:
        if not p.exists():
            raise ToolError(f"file not found: {p}")
    return paths
