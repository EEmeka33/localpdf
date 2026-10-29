"""Table reconstruction from words.

Off-the-shelf table finders work on ruling lines and characters, and break on real-world
PDFs: rows without separators get merged ("Alabama\\nAlaska…"), punctuation is split off
("4144\\n,"), fake-bold text (every glyph printed twice) becomes "D DH HA", and tables drawn
rotated inside a landscape page come out scrambled. Here we work from whole *words* instead:

1. take the words in the page's dominant reading direction (rotated text is turned upright),
   dropping fake-bold duplicates;
2. cluster them into visual lines, and split each line into phrases at wide gaps;
3. consecutive lines with several phrases form a table block; its columns are the x-ranges
   covered by the phrases of its fullest rows;
4. phrases are assigned to columns and wrapped continuation lines are merged into their row.
"""
from __future__ import annotations

import re
from collections import Counter
from statistics import median

import pymupdf as fitz

Word = tuple  # (x0, y0, x1, y1, text)


def page_words(page: fitz.Page) -> list[Word]:
    """Words in reading orientation: rotated content is mapped so lines run left→right, top→bottom."""
    dirs = Counter()
    for b in page.get_text("dict")["blocks"]:
        for line in b.get("lines", []):
            dx, dy = line["dir"]
            dirs[(round(dx), round(dy))] += len(line["spans"])
    d = dirs.most_common(1)[0][0] if dirs else (1, 0)
    W, H = page.rect.width, page.rect.height
    out, seen = [], set()
    for x0, y0, x1, y1, text, *_ in page.get_text("words"):
        if d == (0, -1):  # text runs bottom→top: lines advance to the right
            x0, y0, x1, y1 = H - y1, x0, H - y0, x1
        elif d == (0, 1):  # text runs top→bottom: lines advance to the left
            x0, y0, x1, y1 = y0, W - x1, y1, W - x0
        elif d == (-1, 0):  # upside down
            x0, y0, x1, y1 = W - x1, H - y1, W - x0, H - y0
        key = (text, round(x0 / 2), round(y0 / 2))  # fake bold = same word, offset by a fraction of a point
        if key in seen:
            continue
        seen.add(key)
        out.append((x0, y0, x1, y1, text))
    return out


def _lines(words: list[Word]) -> list[list[Word]]:
    if not words:
        return []
    height = median(w[3] - w[1] for w in words)
    lines: list[list[Word]] = []
    for w in sorted(words, key=lambda w: (w[1] + w[3]) / 2):
        cy = (w[1] + w[3]) / 2
        if lines:
            last = lines[-1]
            ly = sum((v[1] + v[3]) / 2 for v in last) / len(last)
            if abs(cy - ly) < height * 0.45:
                last.append(w)
                continue
        lines.append([w])
    return [sorted(line, key=lambda w: w[0]) for line in lines]


BULLETS = {"\uf0b7": "•", "\uf0a7": "▪", "\uf076": "❖", "\uf0d8": "➢", "\uf0fc": "✓"}


def _phrases(line: list[Word], gap: float) -> list[list]:
    """Split a line into [x0, x1, text, words] phrases wherever the horizontal gap is wide."""
    out: list[list] = []
    for w in line:
        text = BULLETS.get(w[4], w[4])
        w = (*w[:4], text)
        is_bullet = out and len(out[-1][2]) == 1 and not out[-1][2].isalnum()  # "•  text" is one phrase
        if out and (w[0] - out[-1][1] < gap or is_bullet):
            out[-1][1] = max(out[-1][1], w[2])
            out[-1][2] += " " + w[4]
            out[-1][3].append(w)
        else:
            out.append([w[0], w[2], w[4], [w]])
    return out


def _columns(rows: list[list[list]]) -> list[tuple[float, float]]:
    """Column x-ranges from the fullest *data* rows.

    Header labels are often centred or span several columns, so when enough rows contain
    digits (= data rows) only those define the columns; headers are then fitted in.
    """
    most = max(len(r) for r in rows)
    ref = [r for r in rows if len(r) >= max(2, most * 0.7)] or rows
    data = [r for r in rows if len(r) >= 2 and any(ch.isdigit() for p in r for ch in p[2])]
    if len(data) >= 3:
        most = max(len(r) for r in data)
        ref = [r for r in data if len(r) >= max(2, most * 0.7)]
    spans = sorted((p[0], p[1]) for r in ref for p in r)
    cols: list[list[float]] = []
    for a, b in spans:
        if cols and a <= cols[-1][1] + 0.5:
            cols[-1][1] = max(cols[-1][1], b)
        else:
            cols.append([a, b])
    return [(a, b) for a, b in cols]


def _column_of(x0, x1, cols) -> int:
    overlap = [max(0.0, min(x1, b) - max(x0, a)) for a, b in cols]
    if max(overlap) > 0:
        return overlap.index(max(overlap))
    mid = (x0 + x1) / 2  # in a gutter: nearest column
    return min(range(len(cols)), key=lambda k: min(abs(mid - cols[k][0]), abs(mid - cols[k][1])))


def _assign(rows: list[list[list]], cols) -> list[list[str]]:
    table = []
    for r in rows:
        slots: list[list[tuple]] = [[] for _ in cols]
        for x0, x1, text, words in r:
            touched = [k for k, (a, b) in enumerate(cols) if min(x1, b) - max(x0, a) > 2]
            # a phrase straddling columns (e.g. tightly set headers "Unit price Total") is split word by word
            parts = [(w[0], w[2], w[4]) for w in words] if len(touched) > 1 and len(words) > 1 else [(x0, x1, text)]
            for part in parts:
                slots[_column_of(part[0], part[1], cols)].append(part)
        # separate phrases colliding in one column while a neighbour is empty: headers sitting in the
        # gutter next to right-aligned numbers ("Unit price", "Total") belong to different columns
        for _ in range(len(cols)):
            moved = False
            for k, slot in enumerate(slots):
                if len(slot) < 2:
                    continue
                slot.sort()
                if k + 1 < len(slots) and not slots[k + 1]:
                    slots[k + 1].append(slot.pop())
                    moved = True
                elif k > 0 and not slots[k - 1]:
                    slots[k - 1].append(slot.pop(0))
                    moved = True
            if not moved:
                break
        table.append([" ".join(t for *_, t in sorted(slot)) for slot in slots])
    return table


_NUMERIC = re.compile(r"[\s$€£¥(+-]*\d[\d\s,.]*[\s$€£¥)%]*")


def _merge_wrapped(table: list[list[str]]) -> list[list[str]]:
    """Join continuation lines (first cell empty, few cells filled, no numbers) into the row above."""
    out: list[list[str]] = []
    ncols = len(table[0]) if table else 0
    for row in table:
        filled = [c for c in row if c]
        numeric = any(_NUMERIC.fullmatch(c) for c in filled)  # totals/subtotals are rows of their own
        if out and not row[0] and len(filled) <= max(1, ncols // 3) and not numeric:
            out[-1] = [f"{a} {b}".strip() for a, b in zip(out[-1], row)]
        else:
            out.append(row)
    return out


def _is_prose(table: list[list[str]]) -> bool:
    """Multi-column running text (newspaper layout) also lines up in columns – but its cells are sentences."""
    cells = [c for row in table for c in row if c]
    long = sum(1 for c in cells if len(c.split()) > 6)
    return long > len(cells) * 0.4


def page_layout(page: fitz.Page, min_rows: int = 2, min_cols: int = 2) -> list[tuple[str, object]]:
    """The page in reading order: [("table", rows), ("text", line), …]."""
    words = page_words(page)
    if not words:
        return []
    height = median(w[3] - w[1] for w in words)
    gap = height * 0.6  # wider than a space (~0.3 em), narrower than a column gutter
    lines = [_phrases(line, gap) for line in _lines(words)]

    def as_text(rows):
        return [("text", " ".join(p[2] for p in r)) for r in rows]

    items: list[tuple[str, object]] = []
    block: list[list[list]] = []

    def flush():
        nonlocal block
        trailing = []
        if sum(1 for r in block if len(r) >= min_cols) >= min_rows:
            while block and len(block[-1]) < min_cols:  # single-phrase lines after a table …
                trailing.insert(0, block.pop())
            cols = _columns(block)
            while trailing and cols and trailing[0][0][0] > cols[0][1] + 1:  # … unless indented: wrapped cell text
                block.append(trailing.pop(0))
            table = _merge_wrapped(_assign(block, cols)) if len(cols) >= min_cols else None
            items.extend([("table", table)] if table and not _is_prose(table) else as_text(block))
        else:
            items.extend(as_text(block))
        items.extend(as_text(trailing))
        block = []

    singles = 0
    for phrases in lines:
        if len(phrases) >= min_cols:
            block.append(phrases)
            singles = 0
        elif block and singles < 2:  # tolerate wrapped lines inside a table
            block.append(phrases)
            singles += 1
        else:
            flush()
            items.extend(as_text([phrases]))
            singles = 0
    flush()
    return items


def extract_tables(page: fitz.Page, min_rows: int = 2, min_cols: int = 2) -> list[list[list[str]]]:
    return [rows for kind, rows in page_layout(page, min_rows, min_cols) if kind == "table"]
