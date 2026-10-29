# Self-contained image with every dependency (LibreOffice, Ghostscript, OCR, Chromium…)
#   docker build -t localpdf .
#   docker run --rm -p 127.0.0.1:8765:8765 localpdf                       # web UI
#   docker run --rm -v "$PWD:/work" localpdf compress /work/file.pdf    # CLI
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      libreoffice-writer libreoffice-calc libreoffice-impress ghostscript tesseract-ocr ocrmypdf \
      qpdf poppler-utils libpango-1.0-0 libpangoft2-1.0-0 fonts-dejavu fonts-urw-base35 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[all]" waitress \
    && python -m playwright install --with-deps chromium

WORKDIR /work
EXPOSE 8765
ENTRYPOINT ["localpdf"]
CMD ["web", "--host", "0.0.0.0", "--port", "8765"]
