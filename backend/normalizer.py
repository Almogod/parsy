"""
Parsy Backend — Level 3: Output Normalizer
Guarantees a strict, validated schema across all parse results.
Handles: date uniformity, table token standardisation, heading tree validation,
         markdown correctness, and word/char/reading-time metrics.
"""
import re, datetime
from dataclasses import dataclass, field
from base_parser import ParsedBlock, FastParseResult


# ── Date normalizer ────────────────────────────────────────────────────────
_DATE_PATTERNS = [
    (r"\b(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{2,4})\b", "DMY"),
    (r"\b(\d{4})[/\-\.](\d{1,2})[/\-\.](\d{1,2})\b",   "YMD"),
    (r"\b([A-Za-z]{3,9})\s+(\d{1,2}),?\s+(\d{4})\b",   "MDY_TEXT"),
]
_MONTH_MAP = {
    "january":1,"february":2,"march":3,"april":4,"may":5,"june":6,
    "july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,"aug":8,
    "sep":9,"oct":10,"nov":11,"dec":12,
}

def _normalize_date(match: re.Match, fmt: str) -> str:
    try:
        if fmt == "DMY":
            d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif fmt == "YMD":
            y, m, d = int(match.group(1)), int(match.group(2)), int(match.group(3))
        elif fmt == "MDY_TEXT":
            m = _MONTH_MAP.get(match.group(1).lower(), 0)
            d = int(match.group(2))
            y = int(match.group(3))
        else:
            return match.group(0)
        if y < 100: y += 2000
        dt = datetime.date(y, m, d)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return match.group(0)


def normalize_dates(text: str) -> str:
    for pattern, fmt in _DATE_PATTERNS:
        text = re.sub(pattern, lambda m, f=fmt: _normalize_date(m, f), text)
    return text


# ── Heading tree validator ─────────────────────────────────────────────────
def validate_heading_tree(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    """
    Ensures heading levels never jump by more than 1 (e.g. H1→H3 becomes H1→H2).
    This produces a structurally valid document tree.
    """
    last_level = 0
    for b in blocks:
        if b.block_type == "heading" and b.level > 0:
            # If jump is too large, bring it down
            if b.level > last_level + 1:
                b.level = last_level + 1
            last_level = b.level
    return blocks


# ── Table normalizer ───────────────────────────────────────────────────────
def normalize_table(rows: list[list[str]]) -> list[list[str]]:
    """
    - Strips excessive whitespace
    - Makes all rows the same width (pads with empty strings)
    - Removes fully-empty rows
    """
    if not rows:
        return rows
    max_cols = max(len(r) for r in rows)
    cleaned  = []
    for row in rows:
        norm = [re.sub(r"\s+", " ", c).strip() for c in row]
        norm += [""] * (max_cols - len(norm))
        if any(c for c in norm):
            cleaned.append(norm)
    return cleaned


# ── Markdown generator ─────────────────────────────────────────────────────
def blocks_to_markdown(blocks: list[ParsedBlock], include_tables: bool = True) -> str:
    lines  = []
    prev_type = None

    for b in blocks:
        if b.block_type == "heading":
            if prev_type and prev_type != "heading":
                lines.append("")
            lines.append("#" * b.level + " " + b.content)
            lines.append("")

        elif b.block_type == "paragraph":
            lines.append(b.content)
            lines.append("")

        elif b.block_type == "list":
            lines.append("- " + b.content)

        elif b.block_type == "table" and include_tables and b.table_data:
            rows = normalize_table(b.table_data)
            if len(rows) >= 1:
                header = rows[0]
                sep    = ["---"] * len(header)
                body   = rows[1:]
                fmt    = lambda r: "| " + " | ".join(r) + " |"
                lines.append("")
                lines.append(fmt(header))
                lines.append(fmt(sep))
                for row in body:
                    lines.append(fmt(row))
                lines.append("")

        elif b.block_type == "hr":
            lines.append("\n---\n")

        elif b.block_type == "image":
            lines.append(f"\n> 📷 *[Image on page {b.page + 1}]*\n")

        prev_type = b.block_type

    # Collapse multiple blank lines
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return result.strip()


# ── JSON schema output ─────────────────────────────────────────────────────
def blocks_to_json_schema(blocks: list[ParsedBlock],
                          meta: dict, tables: list) -> dict:
    sections = []
    cur: dict | None = None

    for b in blocks:
        if b.block_type == "heading":
            if cur:
                sections.append(cur)
            cur = {"heading": b.content, "level": b.level, "paragraphs": [], "lists": []}
        elif b.block_type == "paragraph" and cur:
            cur["paragraphs"].append(b.content)
        elif b.block_type == "list" and cur:
            cur["lists"].append(b.content)

    if cur:
        sections.append(cur)

    return {
        "metadata": meta,
        "sections": sections,
        "tables": [normalize_table(t) for t in tables],
        "blockCount": len(blocks),
    }


# ── Metrics ────────────────────────────────────────────────────────────────
def compute_metrics(raw_text: str, blocks: list[ParsedBlock],
                    tables: list, meta: dict) -> dict:
    words   = len(raw_text.split())
    chars   = len(raw_text)
    heading_counts = {}
    for b in blocks:
        if b.block_type == "heading":
            key = f"h{b.level}"
            heading_counts[key] = heading_counts.get(key, 0) + 1

    return {
        **meta,
        "wordCount":     words,
        "charCount":     chars,
        "lineCount":     raw_text.count("\n") + 1,
        "tableCount":    len(tables),
        "readingTime":   f"{max(1, words // 200)} min",
        "headings":      heading_counts,
        "language":      _detect_language(raw_text),
        "parsedAt":      datetime.datetime.utcnow().isoformat() + "Z",
    }


def _detect_language(text: str) -> str:
    sample = text[:600].lower()
    if re.search(r"\b(le|la|les|du|et|est|une|dans)\b", sample): return "French"
    if re.search(r"\b(der|die|das|und|ist|ein|eine|nicht)\b", sample): return "German"
    if re.search(r"\b(el|la|los|las|de|que|en|una)\b", sample): return "Spanish"
    if re.search(r"[\u4e00-\u9fff]", sample): return "Chinese"
    if re.search(r"[\u0600-\u06ff]", sample): return "Arabic"
    if re.search(r"[\u0400-\u04ff]", sample): return "Russian"
    if re.search(r"[\u3040-\u30ff]", sample): return "Japanese"
    return "English"


# ── Master normalizer entry point ──────────────────────────────────────────
@dataclass
class NormalizedOutput:
    markdown:  str
    plaintext: str
    json_data: dict
    csv_tables: list[str]          # one CSV string per table
    html:      str
    metrics:   dict
    tables:    list[list[list[str]]]


def normalize(result: FastParseResult, output_format: str = "markdown",
              normalize_dates_flag: bool = True) -> NormalizedOutput:

    blocks = validate_heading_tree(result.blocks)
    tables = [normalize_table(t) for t in result.tables]

    raw = result.raw_text
    if normalize_dates_flag:
        raw = normalize_dates(raw)
        for b in blocks:
            b.content = normalize_dates(b.content)

    markdown  = blocks_to_markdown(blocks)
    plaintext = re.sub(r"^#{1,6}\s+", "", markdown, flags=re.MULTILINE)
    plaintext = re.sub(r"\|[^\n]+\|", "", plaintext)
    plaintext = re.sub(r"\n{3,}", "\n\n", plaintext).strip()

    metrics = compute_metrics(raw, blocks, tables, result.metadata)
    json_data = blocks_to_json_schema(blocks, metrics, tables)

    # CSV: one per table
    csv_tables = []
    import csv, io
    for tbl in tables:
        buf = io.StringIO()
        w   = csv.writer(buf)
        w.writerows(tbl)
        csv_tables.append(buf.getvalue())

    # HTML
    html_lines = ["<!DOCTYPE html><html><head><meta charset='UTF-8'>",
                  "<style>body{font-family:system-ui;max-width:800px;margin:2rem auto;",
                  "line-height:1.7;color:#111}table{border-collapse:collapse;width:100%}",
                  "td,th{border:1px solid #ddd;padding:8px}th{background:#f5f5f5}</style></head><body>"]
    for b in blocks:
        if b.block_type == "heading":
            html_lines.append(f"<h{b.level}>{b.content}</h{b.level}>")
        elif b.block_type == "paragraph":
            html_lines.append(f"<p>{b.content}</p>")
        elif b.block_type == "list":
            html_lines.append(f"<li>{b.content}</li>")
        elif b.block_type == "table" and b.table_data:
            rows = normalize_table(b.table_data)
            html_lines.append("<table><thead><tr>" +
                "".join(f"<th>{c}</th>" for c in rows[0]) + "</tr></thead><tbody>")
            for row in rows[1:]:
                html_lines.append("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>")
            html_lines.append("</tbody></table>")
    html_lines.append("</body></html>")

    return NormalizedOutput(
        markdown=markdown, plaintext=plaintext, json_data=json_data,
        csv_tables=csv_tables, html="\n".join(html_lines),
        metrics=metrics, tables=tables
    )
