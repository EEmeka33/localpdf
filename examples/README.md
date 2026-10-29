# Conversion examples and evaluation

Every conversion in localpdf has been run on realistic documents. Each output is scored automatically and was
also **inspected by eye**. You can judge for yourself: the inputs are in [`inputs/`](inputs), the results
in [`outputs/`](outputs), and a before/after picture of every job in [`previews/`](previews).

```bash
python examples/make_inputs.py     # regenerate the synthetic documents (fixed random seed)
```

```bash
python examples/run_examples.py    # run all 29 jobs → outputs/, previews/, RESULTS.md, results.json
```

## The test documents

**Real-world PDFs** ([`inputs/realworld/`](inputs/realworld)): these are public-domain US federal documents
from [pdfplumber's test corpus](https://github.com/jsvine/pdfplumber/tree/stable/tests/pdfs). They are the kind of
messy files that break converters.

| File | Why it is hard |
|---|---|
| `federal-register-2020-17221.pdf` | 15 pages of dense **3-column** text; margin notes printed sideways |
| `nics-background-checks-2015-11.pdf` | FBI statistics: **55 rows × 25 columns** with no lines between the rows |
| `senate-expenditures.pdf` | the table is **drawn rotated 90°** inside the page, and every glyph is printed twice (fake bold) |
| `scotus-transcript-p1.pdf` | Supreme Court transcript: line numbers, monospaced layout |

**Generated documents** ([`make_inputs.py`](make_inputs.py)), with randomized names, numbers and text:

- a 3-page Word report (header/footer with page field, lists, styled table, chart)
- a 2-sheet Excel workbook (formulas, € formats, merged title, native chart) and a CSV
- a 16:9 PowerPoint deck (native chart, table, shapes, picture)
- a small **website folder** (CSS grid, SVG logo, a canvas chart drawn by JavaScript) and an invoice `.html` with `@page` rules
- a CV laid out as an A4 card in HTML (responsive breakpoint wider than A4, more content than one page)
- Markdown, a 260-line log file and an RTF letter
- images: WEBP, JPEG, a transparent PNG, a photo with an EXIF rotation flag, and a 3-page Group-4 fax TIFF
- a **phone photo of a document** (tilted, unevenly lit, on a desk) and a noisy **2-page scan** with its ground-truth text

## Results

Grades are my visual judgement of the output: **A** = you would ship it, **B** = usable with small flaws,
**C** = needs manual work. The automatic metrics are explained [below](#how-the-metrics-work) and listed in full in
[RESULTS.md](RESULTS.md).

### Convert to PDF

| # | Conversion | Output | Key metrics | Grade | Notes |
|---|---|---|---|:-:|---|
| 01 | [Word → PDF](previews/01-word-to-pdf.jpg) | [report.pdf](outputs/01-word-to-pdf/report.pdf) | text 100% · 1.5 s | **A** | Headers, page-number field, table style and chart are all kept (LibreOffice). |
| 02 | [Excel → PDF](previews/02-excel-to-pdf.jpg) | [invoices.pdf](outputs/02-excel-to-pdf/invoices.pdf) | text 100% · 2 pages | **A** | Formulas are calculated and € formats kept. *Fixed during this evaluation:* wide sheets used to print their last columns on separate pages (6 pages); the new `--fit width` default keeps every column on the page. |
| 03 | [CSV → PDF](previews/03-csv-to-pdf.jpg) | [weather.pdf](outputs/03-csv-to-pdf/weather.pdf) | text 100% | **A** | |
| 04 | [PowerPoint → PDF](previews/04-powerpoint-to-pdf.jpg) | [pitch.pdf](outputs/04-powerpoint-to-pdf/pitch.pdf) | text 100% · 5 slides | **A** | The native chart, table and rounded shapes render exactly. |
| 05 | [HTML folder → PDF](previews/05-html-folder-to-pdf.jpg) | [website-a4.pdf](outputs/05-html-folder-to-pdf/website-a4.pdf) | JS ran ✔ · 2.1 s | **A** | Local CSS, SVG, images and the **JavaScript canvas chart** all render, and the site's own print CSS is respected (it hides the nav). This is the "HTML file, not only URL" feature. |
| 06 | [HTML → one long page](previews/06-html-single-page.jpg) | [website-single.pdf](outputs/06-html-single-page/website-single.pdf) | 1 page · JS ran ✔ | **A** | Screen CSS, like a full-page screenshot but with selectable text. *Fixed:* the page came out slightly too short and spilled onto a 2nd page. |
| 07 | [HTML via WeasyPrint](previews/07-html-weasyprint.jpg) | [website-weasyprint.pdf](outputs/07-html-weasyprint/website-weasyprint.pdf) | JS ran ✘ | **B** | The fallback engine for when there is no browser: CSS layout is excellent but it cannot run JavaScript, so the canvas chart is empty. |
| 08 | [Invoice .html → PDF](previews/08-html-invoice.jpg) | [invoice.pdf](outputs/08-html-invoice/invoice.pdf) | text 100% · 2 pages | **A** | `@page` size/margins and the forced page break are honoured. |
| 08b | [CV designed as an A4 card](previews/08b-html-designed-sheet.jpg) | [resume.pdf](outputs/08b-html-designed-sheet/resume.pdf) | 210×297 mm · 2 columns ✔ · links 2/2 | **A** | Rendered exactly like the browser shows it, on one A4 page. *Fixed after a user report:* such pages used to print in their **mobile layout over 2 pages**, because the CSS breakpoint (230 mm) is wider than A4 and the card overflows the page. The card's content is ~24% taller than A4, so it is shrunk to fit (`--sheet-fit grow` keeps 1:1). |
| 09 | [Images → PDF](previews/09-images-to-pdf.jpg) | [images.pdf](outputs/09-images-to-pdf/images.pdf) | 4 A4 pages | **A** | WEBP, JPEG and transparent PNG work, and the EXIF-rotated photo comes out upright. Each page is turned to landscape automatically to fit its image. |
| 10 | [Fax TIFF → PDF](previews/10-tiff-fax-to-pdf.jpg) | [fax.pdf](outputs/10-tiff-fax-to-pdf/fax.pdf) | 3 pages | **A** | *Fixed:* this **crashed** ("Overly large image") because the TIFF declares 1 × 1 DPI. Implausible DPI values are now ignored. |
| 11 | [Phone photo → scan](previews/11-scan-photo-to-pdf.jpg) | [scan.pdf](outputs/11-scan-photo-to-pdf/scan.pdf) | 176 words searchable | **B+** | Lighting flattened to crisp black & white, then OCR. *Fixed:* the page used to stay tilted with a thick black desk border, and OCR found only 36 words. Automatic **deskew** and **crop-to-paper** are new. There is no perspective correction yet, so shoot roughly from above. |
| 12 | [Markdown → PDF](previews/12-markdown-to-pdf.jpg) | [readme.pdf](outputs/12-markdown-to-pdf/readme.pdf) | text 94% | **A** | Tables, code, quotes and the image render well. The "missing" 6% is the link URL, the image path and the code-fence language name, none of which should be printed. *Fixed:* task lists now show ☑ / ☐ instead of `[x]`. |
| 13 | [Log file → PDF](previews/13-text-to-pdf.jpg) | [server-log.pdf](outputs/13-text-to-pdf/server-log.pdf) | text 100% · 10 pages | **A** | *Improved:* long lines now wrap with a hanging indent, so a wrapped line can't be mistaken for a new one. |
| 14 | [RTF → PDF](previews/14-rtf-to-pdf.jpg) | [letter.pdf](outputs/14-rtf-to-pdf/letter.pdf) | text 100% | **A** | |

### Convert from PDF

| # | Conversion | Output | Key metrics | Grade | Notes |
|---|---|---|---|:-:|---|
| 15 | [PDF → JPG](previews/15-pdf-to-jpg.jpg) | [federal-register-jpg/](outputs/15-pdf-to-jpg/federal-register-jpg) | 2 pages @110 dpi | **A** | |
| 16 | [Extract images](previews/16-pdf-extract-images.jpg) | [report_p1_img111.png](outputs/16-pdf-extract-images/report-images) | 1 image | **A** | The original chart comes back pixel-for-pixel; tiny decorations are skipped. |
| 17 | [PDF → Word (3-column real doc)](previews/17-pdf-to-word-realworld.jpg) | [federal-register.docx](outputs/17-pdf-to-word-realworld/federal-register.docx) | text 97% · visual 90% · 33 s | **B−** | All the text is there and editable, and the layout is rebuilt as real Word page columns, but the 3-column flow is only approximated: some paragraphs and line breaks shift. This is the hardest case for any PDF→Word converter. *Fixed:* this took **519 s** for 3 pages. pdf2docx's borderless-table detection was the culprit; it now gets an 8 s/page budget and is skipped if it overruns. |
| 18 | [PDF → Word (round trip)](previews/18-pdf-to-word-roundtrip.jpg) | [report-roundtrip.docx](outputs/18-pdf-to-word-roundtrip/report-roundtrip.docx) | text 100% · visual 99% | **A−** | Practically identical to the original Word file, including the table. The bullets use Word's Symbol font, so they show as boxes in LibreOffice on Linux; MS Word has that font. |
| 19 | [PDF → PowerPoint (image)](previews/19-pdf-to-pptx-image.jpg) | [pitch-image.pptx](outputs/19-pdf-to-pptx-image/pitch-image.pptx) | visual 99% | **A** | Pixel-exact slides with the slide text in the speaker notes. Not editable by design. |
| 20 | [PDF → PowerPoint (editable)](previews/20-pdf-to-pptx-editable.jpg) | [pitch-editable.pptx](outputs/20-pdf-to-pptx-editable/pitch-editable.pptx) | visual 99% | **A−** | Looks identical, and text and tables are editable text boxes. The chart becomes vector shapes, not a native chart with data. |
| 21 | [PDF → Excel (FBI table)](previews/21-pdf-to-excel-nics.jpg) | [nics.xlsx](outputs/21-pdf-to-excel-nics/nics.xlsx) | **99% of numbers → numeric cells** | **A−** | One state per row, 25 typed numeric columns, plus the title and footnotes. *Rewritten during this evaluation:* the old extractor merged 5 states into one cell and split "4,144" into `4144` + `,` (2% numeric recall). |
| 22 | [PDF → Excel (rotated Senate ledger)](previews/22-pdf-to-excel-senate.jpg) | [senate.xlsx](outputs/22-pdf-to-excel-senate/senate.xlsx) | numbers 78% · text 98% | **B** | The rotated, double-printed table comes out clean: document no., dates, payee, description, amount. Wrapped descriptions are joined into their row. The top salary section uses a different column layout, so its cells sit in neighbouring columns. The 78% is mostly numbers inside descriptions ("JUL. 15"), which are correctly kept as text. *Was unusable before:* everything landed in one cell as "D DH HA AW W2…". |
| 23 | [PDF → Excel (round trip)](previews/23-pdf-to-excel-roundtrip.jpg) | [invoices-roundtrip.xlsx](outputs/23-pdf-to-excel-roundtrip/invoices-roundtrip.xlsx) | **100% numbers** · text 96% | **A−** | All 6 columns, € amounts as real currency cells, TOTAL row kept. Chart axis labels from the 2nd sheet end up as a few stray cells. |
| 24 | [PDF → PDF/A-2b](previews/24-pdf-to-pdfa.jpg) | [scotus-pdfa.pdf](outputs/24-pdf-to-pdfa/scotus-pdfa.pdf) | PDF/A id ✔ · OutputIntent ✔ · fonts 3/3 embedded | **B+** | Looks identical and has all the PDF/A markers. Not checked with an official validator (veraPDF isn't available here), so "probably conformant". |
| 25 | [PDF → text (3 columns)](previews/25-pdf-to-text.jpg) | [federal-register.txt](outputs/25-pdf-to-text/federal-register.txt) | text 100% | **A** | *Fixed:* text was read straight across all three columns, mixing sentences. A new XY-cut layout analysis reads column by column (also used by the summarizer). |
| 26 | [PDF → HTML](previews/26-pdf-to-html.jpg) | [scotus.html](outputs/26-pdf-to-html/scotus.html) | text 100% | **A−** | Positioned like the original and the text is selectable. *Fixed:* the CSS needed for positioning was missing, so the line numbers piled up above the text. |
| 27 | [PDF → Markdown](previews/27-pdf-to-markdown.jpg) | [report.md](outputs/27-pdf-to-markdown/report.md) | text 96% | **B+** | Headings, lists and the table are recovered, and running headers/footers are dropped. Table cells keep `**bold**` markers. *Fixed:* it ran OCR and printed engine messages. |
| 28 | [OCR (noisy scan)](previews/28-ocr.jpg) | [scanned-ocr.pdf](outputs/28-ocr/scanned-ocr.pdf) | **99.3% character accuracy** | **A** | Measured against the ground-truth text. *Fixed:* the output was **14× bigger** than the scan (6.3 MB) because pages were re-rendered. An invisible text layer is now laid over the untouched page (565 KB). |

### Not evaluated here

- **AI summarize / translate / ask with an LLM**: these need a local model (Ollama) or an Argos language pack, and
  neither is available in the evaluation environment. The offline extractive summarizer and the
  layout-preserving translation pipeline are covered by the unit tests.
- **HTML from a live URL**: outbound web access was blocked here. URL loading is tested against a local web server
  in the unit tests.

## Bugs this evaluation found (all fixed, with regression tests)

1. `jpg-to-pdf` crashed on TIFFs declaring a bogus 1 × 1 DPI.
2. `excel-to-pdf` printed the last columns of wide sheets on separate pages → new `--fit width|page|none`.
3. `pdf-to-word` could take ~3–5 minutes per page on dense text → time-boxed table detection, `--tables`.
4. `pdf-to-excel` merged rows, split numbers, failed on rotated/fake-bold tables, dropped headings → new
   word-based table engine (`localpdf/tables.py`), typed numbers (thousands separators incl. spaces, %, currency,
   accounting negatives), one sheet per page with text kept.
5. `pdf-to-text` and the summarizer mixed multi-column text → XY-cut reading order.
6. `pdf-to-html` lost its positioning.
7. `ocr` output was 14× larger than its input, and `--force` silently produced no text.
8. `scan-to-pdf` didn't straighten or crop photos → deskew + crop-to-paper.
9. `html-to-pdf --paper single` spilled onto a second page.
10. `markdown-to-pdf` printed `[x]`, and `text-to-pdf` wrapped lines without an indent.
11. `pdf-to-markdown` ran OCR and printed engine messages.
12. `html-to-pdf` printed HTML pages designed as A4 sheets (CVs) in their mobile layout over 2 pages → sheet
    detection renders them exactly as on screen (`--sheets`, `--sheet-fit`).

## How the metrics work

| Metric | Meaning |
|---|---|
| text recall | share of the source's words found in the output (order-insensitive; thousands separators normalized) |
| visual similarity | 1 − mean pixel difference between source and output pages (round trips; outputs rendered with LibreOffice) |
| numeric cell recall | share of the numbers printed in the PDF (whole words only, so dates and IDs don't count) that became numeric spreadsheet cells with the same value |
| OCR character accuracy | similarity between the OCR text layer and the known ground truth |
| PDF/A markers | XMP `pdfaid` declaration, an sRGB OutputIntent and embedded fonts |

The previews render each output in the application you'd open it with: LibreOffice for DOCX/PPTX, our own
`excel-to-pdf --fit width` for XLSX, and Chromium for HTML.
