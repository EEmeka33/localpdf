"""PDF Intelligence: summarize & translate – 100 % local.

Engines
* ``extractive`` – built-in, offline sentence ranking (no model needed)
* ``argos``      – offline neural translation (``pip install argostranslate``)
* ``ollama``     – any local LLM served by Ollama (https://ollama.com), e.g. llama3.2, qwen2.5, mistral
"""
from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

import pymupdf as fitz

from ..registry import Opt, tool
from ..utils import ToolError, open_pdf, parse_pages, reading_order, save_pdf

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL
DEFAULT_MODEL = os.environ.get("LOCALPDF_MODEL", "llama3.2")

STOPWORDS = set("""a about above after again against all am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here
hers herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on once
only or other our ours ourselves out over own same she should so some such than that the their theirs them themselves
then there these they this those through to too under until up very was we were what when where which while who whom why
will with would you your yours yourself yourselves also may might must shall le la les de des du un une et est en que qui
dans pour pas sur au aux il elle ils elles ce cette ces der die das und ist nicht ein eine el los las y es por con para""".split())


def ollama(prompt: str, model: str | None = None, system: str | None = None) -> str:
    body = {"model": model or DEFAULT_MODEL, "prompt": prompt, "stream": False, "options": {"temperature": 0.2}}
    if system:
        body["system"] = system
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        # local server: never route through a proxy
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=900) as resp:
            return json.loads(resp.read())["response"].strip()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        if "not found" in detail:
            raise ToolError(f"Ollama model '{body['model']}' is not pulled – run `ollama pull {body['model']}`") from None
        raise ToolError(f"Ollama error: {detail}") from None
    except (urllib.error.URLError, ConnectionError) as e:
        raise ToolError(f"cannot reach Ollama at {OLLAMA_URL} – install it from https://ollama.com and run "
                        f"`ollama serve` (or use the offline engine). ({e})") from None


def _document_text(path, pages="all") -> str:
    doc = open_pdf(path)
    text = "\n".join("\n".join(reading_order(doc[i])) for i in parse_pages(pages, doc.page_count))
    if not text.strip():
        raise ToolError("no text found – scanned document? run `localpdf ocr` first")
    return text


def _sentences(text: str) -> list[str]:
    text = re.sub(r"-\n(\w)", r"\1", text)  # de-hyphenate
    text = re.sub(r"\s*\n\s*", " ", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(«¿¡])", text)
    return [p.strip() for p in parts if 25 <= len(p.strip()) <= 600]


def extractive_summary(text: str, n: int) -> list[str]:
    """Classic TF-IDF-ish sentence scoring with position bonus; returns sentences in original order."""
    sents = _sentences(text)
    if len(sents) <= n:
        return sents
    words = [[w for w in re.findall(r"[^\W\d_]{3,}", s.lower()) if w not in STOPWORDS] for s in sents]
    freq = Counter(w for ws in words for w in ws)
    if not freq:
        return sents[:n]
    top = freq.most_common(1)[0][1]
    df = Counter(w for ws in words for w in set(ws))
    scores = []
    for i, ws in enumerate(words):
        if not ws:
            scores.append(0)
            continue
        s = sum((freq[w] / top) * math.log(1 + len(sents) / df[w]) for w in ws) / (len(ws) ** 0.6)
        s *= 1.25 if i < len(sents) * 0.1 else 1.0  # intro sentences tend to matter
        scores.append(s)
    chosen = sorted(sorted(range(len(sents)), key=lambda i: -scores[i])[:n])
    return [sents[i] for i in chosen]


def _keywords(text: str, n=12) -> list[str]:
    words = [w for w in re.findall(r"[^\W\d_]{4,}", text.lower()) if w not in STOPWORDS]
    return [w for w, _ in Counter(words).most_common(n)]


@tool(
    name="summarize", title="AI Summarizer", category="PDF Intelligence",
    help="Summarize long PDFs into key points – offline extractive engine, or a local LLM through Ollama.",
    aliases=("summary",), output="md",
    opts=[
        Opt("engine", "choice", "extractive", "Summarizer.", ["extractive", "ollama"]),
        Opt("length", "choice", "medium", "Summary length.", ["short", "medium", "long"]),
        Opt("model", "str", None, f"Ollama model (default {DEFAULT_MODEL}, or $LOCALPDF_MODEL)."),
        Opt("language", "str", None, "Ollama: write the summary in this language (default: document language)."),
        Opt("pages", "str", "all", "Pages to summarize."),
    ],
)
def summarize(inputs, output: Path, engine="extractive", length="medium", model=None, language=None, pages="all"):
    text = _document_text(inputs[0], pages)
    title = Path(inputs[0]).stem
    words = len(text.split())
    if engine == "ollama":
        bullets = {"short": "3-5", "medium": "6-10", "long": "12-20"}[length]
        chunks = _chunks(text, 12000)
        partials = []
        for i, chunk in enumerate(chunks, 1):
            if len(chunks) > 1:
                print(f"  summarizing part {i}/{len(chunks)}…")
            partials.append(ollama(f"Summarize this part of a document as concise bullet points:\n\n{chunk}", model))
        lang = f" Write in {language}." if language else " Write in the same language as the document."
        body = ollama(
            f"Here are notes from a document titled '{title}'. Write a summary in Markdown with: a one-paragraph "
            f"overview, then '## Key points' with {bullets} bullet points, then '## Conclusion'.{lang}\n\n"
            + "\n\n".join(partials), model)
    else:
        n = {"short": 5, "medium": 10, "long": 20}[length]
        sents = extractive_summary(text, n)
        kw = ", ".join(_keywords(text))
        body = "## Key points\n\n" + "\n".join(f"- {s}" for s in sents) + f"\n\n## Keywords\n\n{kw}\n"
    md = f"# Summary – {title}\n\n_{words:,} words · {engine} engine_\n\n{body}\n"
    output.write_text(md, encoding="utf-8")
    print(md)
    return [output]


def _chunks(text: str, size: int) -> list[str]:
    paras, chunks, cur = text.split("\n"), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            chunks.append(cur)
            cur = ""
        cur += p + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


# ------------------------------------------------------------------ translate
@tool(
    name="translate", title="Translate PDF", category="PDF Intelligence",
    help="Translate PDFs keeping the original layout – offline with Argos Translate or a local LLM via Ollama.",
    requires=("argostranslate", "ollama"),
    opts=[
        Opt("to", "str", "en", "Target language code (en, fr, es, de, it, pt, nl, pl, ru, zh, ja, ar…).", required=True),
        Opt("source", "str", "auto", "Source language code ('auto' guesses it, needed for Argos)."),
        Opt("engine", "choice", "auto", "Translator.", ["auto", "argos", "ollama"]),
        Opt("model", "str", None, "Ollama model."),
        Opt("output_type", "choice", "pdf", "Translated PDF (layout kept) or a plain text file.", ["pdf", "txt"]),
        Opt("pages", "str", "all", "Pages to translate."),
    ],
)
def translate(inputs, output: Path, to="en", source="auto", engine="auto", model=None, output_type="pdf", pages="all"):
    doc = open_pdf(inputs[0])
    page_ids = parse_pages(pages, doc.page_count)
    if source == "auto":
        source = _guess_language(" ".join(doc[i].get_text() for i in page_ids[:3]))
        print(f"detected source language: {source}")
    translator = _translator(engine, source, to, model)
    if output_type == "txt":
        out = output.with_suffix(".txt")
        texts = [translator(doc[i].get_text(sort=True)) for i in page_ids]
        out.write_text("\n\f\n".join(texts), encoding="utf-8")
        return [out]
    css = "* { font-family: sans-serif; }"
    for i in page_ids:
        page = doc[i]
        blocks = [b for b in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"] if b.get("type") == 0]
        jobs = []
        for b in blocks:
            text = "\n".join("".join(s["text"] for s in line["spans"]) for line in b["lines"]).strip()
            if not text or not re.search(r"[^\W\d_]", text):
                continue
            span = b["lines"][0]["spans"][0]
            rgb = span["color"]
            jobs.append((fitz.Rect(b["bbox"]), text, span["size"], f"#{rgb:06x}", bool(span["flags"] & 16)))
            page.add_redact_annot(fitz.Rect(b["bbox"]), fill=False)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
        for rect, text, size, col, bold in jobs:
            translated = translator(text.replace("\n", " "))
            from html import escape

            html = f"<p style='font-size:{size:.1f}pt;color:{col};{'font-weight:bold;' if bold else ''}margin:0'>" \
                   f"{escape(translated)}</p>"
            page.insert_htmlbox(rect, html, css=css)  # shrinks text automatically if it gets longer
        print(f"  page {i + 1} translated ({len(jobs)} blocks)")
    return save_pdf(doc, output)


def _translator(engine, source, target, model):
    if engine in ("auto", "argos"):
        try:
            import argostranslate.package
            import argostranslate.translate

            def ensure_pkg(src, dst):
                langs = {lang.code: lang for lang in argostranslate.translate.get_installed_languages()}
                if src in langs and dst in langs and langs[src].get_translation(langs[dst]):
                    return
                print(f"downloading offline model {src}→{dst} (first use only)…")
                argostranslate.package.update_package_index()
                pkg = next((p for p in argostranslate.package.get_available_packages()
                            if p.from_code == src and p.to_code == dst), None)
                if pkg is None:
                    raise ToolError(f"Argos has no {src}→{dst} model; try --engine ollama")
                argostranslate.package.install_from_path(pkg.download())

            ensure_pkg(source, target)
            return lambda text: argostranslate.translate.translate(text, source, target)
        except ImportError:
            if engine == "argos":
                raise ToolError("pip install argostranslate") from None
    system = ("You are a professional translator. Translate the user's text into the language with code "
              f"'{target}'. Output only the translation – no notes, no quotes, keep numbers and names.")
    return lambda text: ollama(text, model, system=system)


LANG_HINTS = {
    "en": "the and of to is in that for with", "fr": "le la les des et est une pour dans que",
    "es": "el la los las de que y en un para", "de": "der die das und ist nicht ein zu mit",
    "it": "il di che è la per un non sono", "pt": "o a os de que e não um para com",
    "nl": "de het een en van is dat niet op", "pl": "i w nie na się to że jest z",
}


def _guess_language(text: str) -> str:
    words = Counter(re.findall(r"[^\W\d_]+", text.lower()))
    scores = {code: sum(words[w] for w in hints.split()) for code, hints in LANG_HINTS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] else "en"


# ------------------------------------------------------------------ ask (extra)
@tool(
    name="ask", title="Chat with PDF", category="PDF Intelligence", extra=True,
    help="Ask questions about a PDF and get answers with page references (local LLM via Ollama).",
    output="none", requires=("ollama",),
    opts=[
        Opt("question", "str", None, "Your question.", required=True),
        Opt("model", "str", None, "Ollama model."),
    ],
)
def ask(inputs, output, question, model=None):
    doc = open_pdf(inputs[0])
    pages = [(i + 1, p.get_text()) for i, p in enumerate(doc)]
    q_words = {w for w in re.findall(r"[^\W\d_]{3,}", question.lower()) if w not in STOPWORDS}
    # retrieve the most relevant pages so long documents fit the model's context
    ranked = sorted(pages, key=lambda p: -sum(p[1].lower().count(w) for w in q_words))
    context, size = [], 0
    for num, text in ranked:
        if size > 14000:
            break
        context.append(f"[page {num}]\n{text}")
        size += len(text)
    return ollama(f"Answer the question using only the document excerpts below. Cite pages like (p. 3). "
                  f"If the answer is not there, say so.\n\nQuestion: {question}\n\n" + "\n\n".join(context), model)
