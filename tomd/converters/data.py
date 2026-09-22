"""Structured-data converters: ``csv``, ``json``, ``jsonl``, ``xml``, ``notebook``.

The guiding question for this family is: *what does the consumer need?*

* Feed a human or an LLM a document -> a pipe table preserves row/column
  alignment and is dramatically cheaper to reason about than raw CSV.
* Feed a program -> keep the raw bytes in a fence so nothing is lost.

So these converters emit the readable form **and** keep the original available,
and they always record the shape (rows/columns/keys) in metadata plus the
conversion report.  When a table would be too large to be readable, they say so
and fall back to the fenced form instead of silently emitting 5000 rows.
"""

from __future__ import annotations

import csv as _csv
import io
import json
import re
from pathlib import Path
from typing import Any

from .. import mdutil as md
from ..model import Document, Status
from ..registry import registry

DELIMITERS = [",", "\t", ";", "|", ":"]


def _sniff_delimiter(sample: str, fallback: str = ",") -> str:
    lines = [ln for ln in sample.splitlines() if ln.strip()][:20]
    if not lines:
        return fallback
    best, best_score = fallback, -1.0
    for delim in DELIMITERS:
        counts = [ln.count(delim) for ln in lines]
        if min(counts) < 1:
            continue
        mean = sum(counts) / len(counts)
        spread = (max(counts) - min(counts)) / mean if mean else 1.0
        score = mean - spread * mean
        if score > best_score:
            best, best_score = delim, score
    return best


def _read_rows(text: str, delimiter: str, limit: int | None = None) -> list[list[str]]:
    reader = _csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for index, row in enumerate(reader):
        if limit is not None and index >= limit:
            break
        rows.append(row)
    return rows


@registry.converter("csv", "delimited", priority=10, description="CSV/TSV -> pipe table")
def convert_csv(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)

    delimiter = options.get("delimiter") or _sniff_delimiter(read.text)
    max_rows = int(options.get("csv_max_rows", 50))
    max_cols = int(options.get("csv_max_cols", 16))
    has_header = bool(options.get("csv_header", True))

    try:
        rows = _read_rows(read.text, delimiter)
    except _csv.Error as exc:
        doc.warn("csv-parse-failed", f"CSV parser rejected the file ({exc}); emitting raw text")
        doc.add(md.fence(read.text, "csv"))
        return doc

    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        doc.set_status(Status.EMPTY)
        doc.warn("empty-file", "no data rows found", "error")
        return doc

    width = max(len(r) for r in rows)
    ragged = sum(1 for r in rows if len(r) != width)
    doc.meta.update(
        {
            "delimiter": delimiter,
            "rows": len(rows),
            "columns": width,
            "encoding": read.encoding,
        }
    )
    if read.had_errors:
        doc.warn("encoding-uncertain", f"decoded from {read.encoding} with replacement characters")
    if ragged:
        doc.warn(
            "ragged-rows",
            f"{ragged} row(s) have a different column count than the widest row ({width}); "
            "they were padded with empty cells",
            "info",
        )

    too_big = len(rows) > max_rows + 1 or width > max_cols
    if too_big:
        doc.warn(
            "table-too-large",
            f"{len(rows)} rows x {width} columns exceeds the readable table budget "
            f"({max_rows} x {max_cols}); emitted a fenced block instead of a table. "
            "Raise --csv-max-rows to force a table.",
            "info",
        )
        doc.add(md.heading(2, "Data"))
        doc.add(md.fence(read.text, "csv"))
        doc.add("<details>\n<summary>Preview (first %d rows as a table)</summary>\n\n" % max_rows
                + md.table(rows[: max_rows + 1], header=has_header) + "\n\n</details>")
        return doc

    doc.add(md.table(rows, header=has_header))
    if not has_header:
        doc.warn("no-header", "first row was treated as data (--no-csv-header)", "info")
    return doc


# ---------------------------------------------------------------------- json


def _flatten(value: Any, prefix: str = "", depth: int = 0, max_depth: int = 6) -> dict[str, str]:
    out: dict[str, str] = {}
    if depth > max_depth:
        out[prefix or "(root)"] = "…"
        return out
    if isinstance(value, dict):
        for key, item in value.items():
            out.update(_flatten(item, f"{prefix}.{key}" if prefix else str(key), depth + 1, max_depth))
    elif isinstance(value, list):
        if all(not isinstance(i, (dict, list)) for i in value):
            out[prefix or "(root)"] = ", ".join(md.inline_value(i) for i in value)
        elif value and all(isinstance(i, dict) for i in value):
            out[prefix or "(root)"] = f"[{len(value)} objects]"
            for index, item in enumerate(value[:3]):
                out.update(_flatten(item, f"{prefix}[{index}]", depth + 1, max_depth))
        else:
            out[prefix or "(root)"] = f"[{len(value)} items]"
    else:
        out[prefix or "(root)"] = md.inline_value(value)
    return out


def _tabular(value: Any) -> list[list[str]] | None:
    """Return rows if ``value`` is a list of flat objects (or a flat object)."""
    if isinstance(value, dict) and value and all(
        not isinstance(v, (dict, list)) for v in value.values()
    ):
        return [["Key", "Value"], *[[str(k), str(v)] for k, v in value.items()]]
    if isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
        keys: list[str] = []
        for item in value:
            for key in item:
                if key not in keys:
                    keys.append(key)
        if len(keys) > 24 or any(
            isinstance(v, (dict, list)) for item in value for v in item.values()
        ):
            return None
        return [keys, *[[_cell(item.get(k)) for k in keys] for item in value]]
    return None


def _cell(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return md.inline_value(value)


@registry.converter("json", "json", priority=10, description="JSON with shape summary")
def convert_json(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    text = read.text

    try:
        data = json.loads(text)
    except Exception as exc:  # noqa: BLE001 - truncated or invalid JSON is common
        doc.warn("json-parse-failed", f"invalid JSON ({exc}); emitting raw text")
        doc.add(md.fence(text, "json"))
        return doc

    if read.had_errors:
        doc.warn("encoding-uncertain", f"decoded from {read.encoding} with replacement characters")

    # --- shape summary: the single most useful thing for an LLM to see first.
    kind = type(data).__name__
    summary: list[tuple[str, Any]] = [("Root type", kind)]
    if isinstance(data, dict):
        summary.append(("Keys", len(data)))
        summary.append(("Top-level keys", ", ".join(map(str, list(data)[:20]))))
    elif isinstance(data, list):
        summary.append(("Items", len(data)))
        if data:
            summary.append(("Item type", type(data[0]).__name__))
    doc.add(md.kv_table(summary))
    doc.meta.update({"root_type": kind})
    if isinstance(data, (dict, list)):
        doc.meta["size"] = len(data)

    # --- readable projection.
    rows = _tabular(data)
    prefer_table = bool(options.get("json_table", True))
    if rows and prefer_table:
        max_rows = int(options.get("json_max_rows", 200))
        if len(rows) <= max_rows + 1:
            doc.add(md.heading(2, "Data"))
            doc.add(md.table(rows))
        else:
            doc.warn(
                "table-too-large",
                f"{len(rows)} rows exceeds --json-max-rows={max_rows}; emitted JSON only",
                "info",
            )
    elif rows is not None and not prefer_table:
        pass
    elif rows is None and isinstance(data, (dict, list)):
        flat = _flatten(data)
        if flat and len(flat) <= 400:
            doc.add(md.heading(2, "Fields"))
            doc.add(md.table([["Path", "Value"], *[[k, v] for k, v in flat.items()]]))

    doc.add("<details>\n<summary>Raw JSON</summary>\n\n"
            + md.fence(json.dumps(data, ensure_ascii=False, indent=2), "json")
            + "\n\n</details>")
    return doc


@registry.converter("jsonl", "jsonl", priority=10, description="JSON Lines / NDJSON")
def convert_jsonl(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    objects: list[Any] = []
    bad = 0
    for line in read.text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            objects.append(json.loads(line))
        except Exception:
            bad += 1

    if not objects:
        doc.set_status(Status.EMPTY)
        doc.warn("empty-file", "no valid JSON objects found on any line", "error")
        return doc

    doc.meta["records"] = len(objects)
    doc.add(md.kv_table([("Records", len(objects)), ("Root types",
                        ", ".join(sorted({type(o).__name__ for o in objects}))) ]))
    if bad:
        doc.warn("jsonl-bad-lines", f"{bad} line(s) were not valid JSON and were skipped")

    rows = _tabular(objects)
    max_rows = int(options.get("json_max_rows", 200))
    if rows and len(rows) <= max_rows + 1:
        doc.add(md.heading(2, "Records"))
        doc.add(md.table(rows))
    else:
        doc.warn("table-too-large", f"{len(objects)} records rendered as JSON, not a table", "info")
        body = "\n".join(json.dumps(o, ensure_ascii=False) for o in objects[:max_rows])
        doc.add(md.heading(2, "Records"))
        doc.add(md.fence(body, "json"))
    return doc


# ----------------------------------------------------------------------- xml


@registry.converter("xml", "xml", priority=10, description="XML pretty-printed")
def convert_xml(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    text = read.text

    tags = re.findall(r"<\s*([A-Za-z_][\w.:-]*)", text)
    counts: dict[str, int] = {}
    for tag in tags:
        counts[tag] = counts.get(tag, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:15]
    if top:
        doc.add(md.kv_table([("Root element", tags[0] if tags else ""),
                             ("Elements", len(tags)),
                             ("Distinct tags", len(counts))]))
        doc.add(md.heading(2, "Most frequent elements"))
        doc.add(md.table([["Tag", "Count"], *[[t, c] for t, c in top]]))
    doc.add("<details>\n<summary>Raw XML</summary>\n\n" + md.fence(text, "xml") + "\n\n</details>")
    doc.meta["elements"] = len(tags)
    if not tags:
        doc.warn("no-elements", "no XML elements recognised; treated as plain text")
    return doc


# ------------------------------------------------------------------ notebook


def _notebook_output_text(output: dict) -> str:
    """Convert one notebook output into a markdown fragment."""
    kind = output.get("output_type", "")
    chunks: list[str] = []

    if kind == "stream":
        chunks.append("".join(output.get("text", "")))
    elif kind in ("execute_result", "display_data"):
        data = output.get("data", {})
        if "text/markdown" in data:
            chunks.append("".join(data["text/markdown"]))
        elif "text/plain" in data:
            chunks.append("".join(data["text/plain"]))
        if "image/png" in data:
            chunks.append(f"*(image output, {len(''.join(data['image/png'])) // 4} B base64 — "
                          "not extracted; enable --extract-output-images to write it to assets/)*")
    elif kind == "error":
        tb = output.get("traceback") or []
        cleaned = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in tb]
        chunks.append("\n".join(cleaned) or f"{output.get('ename')}: {output.get('evalue')}")

    if output.get("name"):
        chunks.insert(0, f"<!-- output: {output['name']} -->")
    return "\n".join(chunks).strip()


@registry.converter("notebook", "jupyter", priority=10, description="Jupyter notebook")
def convert_notebook(path: Path, options: dict) -> Document:
    doc = Document()
    read = md.read_text(path)
    try:
        nb = json.loads(read.text)
    except Exception as exc:  # noqa: BLE001
        doc.warn("notebook-parse-failed", f"invalid notebook JSON ({exc}); emitting raw text")
        doc.add(md.fence(read.text, "json"))
        return doc

    cells = nb.get("cells", [])
    meta = nb.get("metadata", {})
    kernel = (meta.get("kernelspec") or {}).get("name") or (meta.get("language_info") or {}).get("name")
    if kernel:
        doc.meta["kernel"] = kernel

    lang = (meta.get("language_info") or {}).get("name", "python")
    doc.meta["cells"] = len(cells)
    doc.meta["code_cells"] = sum(1 for c in cells if c.get("cell_type") == "code")

    include_outputs = bool(options.get("notebook_outputs", True))
    for index, cell in enumerate(cells, start=1):
        kind = cell.get("cell_type")
        source = "".join(cell.get("source", []))
        if kind == "markdown":
            doc.add(source)
        elif kind == "code":
            doc.add(md.fence(source, lang))
            if include_outputs:
                for output in cell.get("outputs", []):
                    rendered = _notebook_output_text(output)
                    if rendered:
                        doc.add("**Output:**\n\n" + md.fence(rendered, "text"))
        elif kind == "raw":
            doc.add(md.fence(source, "text"))
        else:
            doc.warn("unknown-cell-type", f"cell {index} has unknown type '{kind}'", "info")

    if not cells:
        doc.set_status(Status.EMPTY)
        doc.warn("no-cells", "the notebook contains no cells", "error")
    return doc
