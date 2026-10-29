"""Command-line interface – generated from the tool registry."""
from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path

from . import __version__
from .registry import CATEGORIES, load_all
from .runner import run_tool
from .utils import ToolError, which

EPILOG = """examples:
  localpdf merge a.pdf b.pdf scan.jpg -o all.pdf
  localpdf split big.pdf --mode ranges --ranges "1-3;4-10"
  localpdf compress *.pdf --level extreme
  localpdf html-to-pdf page.html                 # local file (CSS/images/JS included)
  localpdf html-to-pdf https://example.com --paper single
  localpdf pdf-to-word report.pdf
  localpdf watermark doc.pdf --text CONFIDENTIAL --position tile
  localpdf protect doc.pdf --password s3cret
  localpdf web                                   # open the local web interface

run `localpdf <tool> -h` for the options of a tool, `localpdf list` for all tools."""

# what each optional dependency gives you, and how to install it
DEPENDENCIES = [
    ("soffice", "bin", "Word/PowerPoint/Excel ⇄ PDF", "apt install libreoffice  |  dnf install libreoffice  |  pacman -S libreoffice-fresh"),
    ("gs", "bin", "compress (best), PDF/A, grayscale", "apt install ghostscript"),
    ("tesseract", "bin", "OCR, searchable scans", "apt install tesseract-ocr tesseract-ocr-fra …"),
    ("ocrmypdf", "bin", "OCR (best quality, deskew), PDF/A", "apt install ocrmypdf  |  pip install ocrmypdf"),
    ("qpdf", "bin", "repair (via pikepdf, bundled)", "apt install qpdf"),
    ("scanimage", "bin", "scan-to-pdf from a physical scanner", "apt install sane-utils"),
    ("pdftotext", "bin", "pdf-to-text --layout", "apt install poppler-utils"),
    ("playwright", "py", "HTML to PDF with Chromium (best)", "pip install playwright && playwright install chromium"),
    ("weasyprint", "py", "HTML to PDF without a browser", "pip install weasyprint"),
    ("pdf2docx", "py", "PDF to Word (best layout)", "pip install pdf2docx"),
    ("pikepdf", "py", "protect / unlock / repair", "pip install pikepdf"),
    ("pyhanko", "py", "cryptographic digital signatures", "pip install pyhanko"),
    ("argostranslate", "py", "offline translation", "pip install argostranslate"),
    ("pymupdf4llm", "py", "better PDF to Markdown", "pip install pymupdf4llm"),
    ("pillow_heif", "py", "HEIC/HEIF photos (iPhone)", "pip install pillow-heif"),
]


def build_parser() -> argparse.ArgumentParser:
    tools = load_all()
    parser = argparse.ArgumentParser(
        prog="localpdf", description="Every iLovePDF tool – and more – running locally on your machine.",
        epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"localpdf {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<tool>")

    sub.add_parser("list", help="List every tool by category.")
    sub.add_parser("doctor", help="Check which optional dependencies are installed.")
    web = sub.add_parser("web", help="Start the local web interface (http://127.0.0.1:8765).")
    web.add_argument("--host", default="127.0.0.1", help="Interface to bind (default: localhost only).")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--open", action="store_true", help="Open the browser automatically.")

    for t in tools.values():
        p = sub.add_parser(t.name, aliases=list(t.aliases), help=t.help, description=f"{t.title}: {t.help}",
                           formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        accepted = ", ".join(t.accept) if t.accept else "any"
        what = "file(s)" + (" or URL" if t.allow_url else "")
        p.add_argument("inputs", nargs="*" if t.min_inputs == 0 else "+", metavar="INPUT",
                       help=f"input {what} ({accepted}). Several files = batch mode for single-file tools.")
        if t.output != "none":
            p.add_argument("-o", "--output", help="Output file (or folder). Default: next to the input.")
        for o in t.opts:
            kw = dict(help=o.help or " ", dest=o.name)
            if o.type == "bool":
                p.add_argument(o.flag, action=argparse.BooleanOptionalAction, default=o.default, **kw)
                continue
            if o.choices:
                kw["choices"] = o.choices
            kw["type"] = {"int": int, "float": float}.get(o.type, str)
            p.add_argument(o.flag, default=o.default, required=o.required, **kw)
        p.set_defaults(_tool=t)
    return parser


def cmd_list() -> None:
    tools = load_all()
    for cat in CATEGORIES:
        items = [t for t in tools.values() if t.category == cat]
        if not items:
            continue
        print(f"\n\033[1m{cat}\033[0m")
        for t in items:
            star = " \033[33m★\033[0m" if t.extra else ""
            print(f"  {t.name:<20} {t.help}{star}")
    print("\n★ = bonus tool not available on iLovePDF")


def cmd_doctor() -> int:
    print(f"localpdf {__version__} – optional dependencies\n")
    missing = 0
    for name, kind, purpose, install in DEPENDENCIES:
        ok = bool(which(name)) if kind == "bin" else importlib.util.find_spec(name) is not None
        if name == "soffice" and not ok:
            ok = bool(which("libreoffice"))
        if name == "playwright" and ok:
            ok = _chromium_ready()
            if not ok:
                install = "playwright install chromium"
        mark = "\033[32m✔\033[0m" if ok else "\033[31m✘\033[0m"
        print(f" {mark} {name:<15} {purpose}")
        if not ok:
            missing += 1
            print(f"     → {install}")
    print("\nTools always work without the missing pieces when a fallback exists; "
          "run ./install.sh to get everything at once.")
    return 0


def _chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists() or bool(which("chromium", "google-chrome"))
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "list":
        cmd_list()
        return 0
    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "web":
        from .web import serve

        serve(args.host, args.port, args.open)
        return 0

    t = args._tool
    options = {o.name: getattr(args, o.name) for o in t.opts}
    try:
        result = run_tool(t, args.inputs, getattr(args, "output", None), options)
    except ToolError as e:
        print(f"localpdf {t.name}: error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if isinstance(result, str):
        print(result)
    else:
        for path in result:
            print(f"✔ {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
