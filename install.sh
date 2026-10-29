#!/usr/bin/env bash
# localpdf installer – system tools + an isolated Python environment.
#
#   ./install.sh                 everything (asks for sudo for system packages)
#   ./install.sh --no-system     only the Python part (no sudo)
#   ./install.sh --ocr-langs "fra deu spa"   extra OCR languages
#   ./install.sh --desktop       also add a "localpdf" launcher to your app menu
set -euo pipefail

PREFIX="${LOCALPDF_HOME:-$HOME/.local/share/localpdf}"
BIN_DIR="$HOME/.local/bin"
SYSTEM=1
DESKTOP=0
OCR_LANGS=""
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-system) SYSTEM=0 ;;
    --desktop) DESKTOP=1 ;;
    --ocr-langs) OCR_LANGS="$2"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

say() { printf '\033[1;31m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"

if [[ $SYSTEM -eq 1 ]]; then
  say "Installing system tools (LibreOffice, Ghostscript, Tesseract, OCRmyPDF, qpdf, poppler, SANE)…"
  if command -v apt-get >/dev/null; then
    langs=""; for l in $OCR_LANGS; do langs+=" tesseract-ocr-$l"; done
    $SUDO apt-get update
    $SUDO apt-get install -y python3 python3-venv python3-pip libreoffice-writer libreoffice-calc \
      libreoffice-impress ghostscript tesseract-ocr $langs ocrmypdf qpdf poppler-utils sane-utils \
      libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu fonts-urw-base35 || true
  elif command -v dnf >/dev/null; then
    langs=""; for l in $OCR_LANGS; do langs+=" tesseract-langpack-$l"; done
    $SUDO dnf install -y python3 python3-pip libreoffice-writer libreoffice-calc libreoffice-impress \
      ghostscript tesseract $langs ocrmypdf qpdf poppler-utils sane-backends pango dejavu-sans-fonts || true
  elif command -v pacman >/dev/null; then
    langs=""; for l in $OCR_LANGS; do langs+=" tesseract-data-$l"; done
    $SUDO pacman -S --needed --noconfirm python python-pip libreoffice-fresh ghostscript tesseract \
      tesseract-data-eng $langs ocrmypdf qpdf poppler sane pango ttf-dejavu || true
  elif command -v zypper >/dev/null; then
    $SUDO zypper install -y python3 python3-pip libreoffice-writer libreoffice-calc libreoffice-impress \
      ghostscript tesseract-ocr ocrmypdf qpdf poppler-tools sane-backends pango || true
  else
    echo "Unknown package manager – install LibreOffice, Ghostscript, Tesseract and qpdf yourself."
  fi
fi

say "Creating Python environment in $PREFIX…"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip wheel >/dev/null
"$PREFIX/venv/bin/pip" install "$HERE[all]"

say "Downloading headless Chromium for HTML → PDF…"
"$PREFIX/venv/bin/python" -m playwright install chromium || \
  echo "   (Chromium download failed – HTML to PDF will fall back to WeasyPrint)"

mkdir -p "$BIN_DIR"
ln -sf "$PREFIX/venv/bin/localpdf" "$BIN_DIR/localpdf"

if [[ $DESKTOP -eq 1 ]]; then
  mkdir -p "$HOME/.local/share/applications"
  cat > "$HOME/.local/share/applications/localpdf.desktop" <<EOF
[Desktop Entry]
Name=localpdf
Comment=Every PDF tool, running locally
Exec=$BIN_DIR/localpdf web --open
Icon=application-pdf
Terminal=false
Type=Application
Categories=Office;Utility;
EOF
  say "Added localpdf to your application menu"
fi

say "Done!"
case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "   Add $BIN_DIR to your PATH:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc" ;; esac
echo "   Try:  localpdf --help      localpdf doctor      localpdf web --open"
