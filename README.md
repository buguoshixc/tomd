# tomd

[![tests](https://github.com/buguoshixc/tomd/actions/workflows/tests.yml/badge.svg)](https://github.com/buguoshixc/tomd/actions/workflows/tests.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

Convert (almost) any file to Markdown, and be honest about what happened.

**中文文档：[README.zh-CN.md](README.zh-CN.md)**

`tomd` is a local-first document converter built around one rule: **never
produce plausible-looking output that is silently wrong.** Every conversion
comes with a report saying which engine ran, what was recovered, and what was
approximated, dropped or refused.

```
$ tomd report.pdf --stdout
# Annual Report 2025

<details open>
<summary>Page 1</summary>

# Annual Report
Prepared by the analytics team

## Summary
Revenue for the year reached 45,300 units, an increase of 12 percent ...

```

## Why another converter

Because the failure mode of existing tools is not "it crashed" -- it is "it
produced a file that looks fine". A benchmark of five open-source PDF to
Markdown converters on real documents found that two of nine tools *silently
changed numbers*, one emitted **zero Markdown headings** (which destroys
heading-based chunking for RAG), and several reported success on scanned PDFs
that they had not actually read.

`tomd` is built so those failures are visible:

| Principle | What it means in practice |
|---|---|
| **Refuse rather than fake** | A scanned PDF with no text layer produces an empty result and an error naming the pages that need OCR. It does not produce a blank `.md` that looks like success. |
| **Report every compromise** | Lossy paths (legacy `.doc`, RTF, fallback HTML engine, missing optional package) are recorded in the conversion report, not hidden. |
| **Structure is the product** | Heading hierarchy is recovered from PDF font metrics and from HTML/Word styles, because markdown without headings is useless for chunking. |
| **Degrade, never explode** | 35 formats, 40 converters, chains of fallbacks. One broken file in a batch of 2 000 is one warning, not a crash. |
| **Zero mandatory dependencies** | The core runs on a bare Python install using only the standard library, then upgrades itself as optional packages appear. |
| **Local and inspectable** | No network calls, no telemetry, ~7 000 lines of Python you can read. Conversion is an I/O primitive, so it is treated like one (see Security). |

## Install

```bash
# Nothing to install: run it straight from the checkout
python scripts/convert.py report.pdf

# Or install the package
pip install -e .
tomd report.pdf
```

Then, for high-fidelity conversion of the common formats:

```bash
pip install -e ".[recommended]"     # PyMuPDF, python-docx, python-pptx, openpyxl, markdownify, ...
pip install -e ".[full]"            # + pypdf, ebooklib, striprtf, lxml, chardet
pip install -e ".[dev]"             # + pytest
```

Every optional package is genuinely optional. `tomd --formats` shows which
engines are available and which fallback is being used instead. Where a missing
package would cost structure rather than fail — YAML without PyYAML, TOML on
Python 3.10 without `tomli` — the file is still emitted in full and the report
says exactly what was lost.

## Use

```bash
tomd report.pdf                      # convert one file -> .md/report.md next to it
tomd report.pdf -o report.md         # ... or to a name you choose
tomd report.pdf --stdout             # print the markdown instead of writing
tomd ./docs --out ./out              # convert a tree, mirroring the structure
tomd ./docs --out ./out --workers 8  # ... in parallel
tomd paper.pdf --stdout --no-report --no-front-matter   # clean output for an LLM
tomd data.csv --csv-max-rows 500     # force a big CSV into a real table
tomd mystery.dat --format text       # override format detection
tomd page.html --html-engine markdownify   # opt into the third-party renderer
tomd --formats                       # what is supported, and by what
tomd --server                        # local web UI on http://127.0.0.1:8765
```

Existing outputs are skipped, so a second run over the same tree is cheap; pass
`--overwrite` to refresh them. `--report-json` additionally writes a
machine-readable `<output>.json` next to each result.

HTML has two renderers. The built-in one is the default (`auto` and `builtin`
both mean it) because it is always available and because output should not
change depending on what happens to be installed; `markdownify` is an explicit
opt-in for its wider tag coverage.

Exit codes: `0` clean, `1` completed with warnings or failures, `2` usage error.
That makes `tomd ./docs --out ./out || echo "check the report"` a usable gate.

As a library:

```python
from tomd import convert, render, ConvertOptions

result = convert("report.pdf")
print(result.status)                    # Status.OK / PARTIAL / EMPTY / FAILED
print(render(result))                   # the markdown text
for warning in result.warnings:
    print(warning.code, warning.message)  # machine-readable + human-readable

# or write it out, assets and report included
convert("report.pdf", ConvertOptions(output_dir="out"), write=True)
```

## Supported formats

| Family | Formats | Notes |
|---|---|---|
| Plain text | `txt` `log` `rst` `adoc` `org` `tex` | Setext headings promoted to ATX; incidental markdown escaped |
| Markdown | `md` `markdown` `mdx` | Front matter lifted into metadata, outline reported |
| Source code | ~50 extensions | Fenced with language + a symbol outline |
| Data | `csv` `tsv` `json` `jsonl` `xml` `yaml` `toml` `ini` `env` `diff` | Tables when readable, fenced source when not; shape summary always |
| Web | `html` `htm` `xhtml` `svg` | Built-in renderer by default, markdownify on request; chrome/content decided by content |
| Word | `docx` `docm` `dotx` | Headings, lists, tables, images, core properties |
| PowerPoint | `pptx` `ppsx` | Slide titles, bullets, tables, speaker notes, images |
| Excel | `xlsx` `xlsm` | Per-sheet tables, dates and types preserved |
| OpenDocument | `odt` `ods` `odp` `odg` | Headings, paragraphs, tables |
| Legacy Office | `doc` `xls` `ppt` `msg` | **Heuristic text recovery only** — always warns |
| Rich text | `rtf` | Text only; formatting is not recovered |
| PDF | `pdf` | Text layer, heading hierarchy from font metrics, tables, images |
| Ebooks | `epub` `fb2` | EPUB in spine order; `mobi`/`azw` refused with a conversion hint |
| Email | `eml` | Decoded headers, text body preferred, attachments listed |
| Notebooks | `ipynb` | Markdown cells, fenced code, outputs, tracebacks |
| Subtitles | `srt` `vtt` `ass` `ssa` | Timestamped cues |
| Images | `png` `jpg` `gif` `webp` `tiff` … | **Metadata only — no OCR** (see Roadmap) |
| Archives | `zip` | Inventoried, not extracted (see Security) |
| Refused | `parquet` `sqlite` `exe` `dll` `bin`, audio, video | Explicit "here is what would work instead" |

## Architecture

```
                       ┌──────────────┐
  file ──────────────► │  detect.py   │  extension + magic bytes + content sniff
                       └──────┬───────┘
                              │  Format(key, media_type, confidence)
                       ┌──────▼───────┐
                       │ registry.py  │  picks the best *installed* converter,
                       │              │  falls back down the chain on failure
                       └──────┬───────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  converters/text.py   converters/office.py   converters/pdf.py   ...
        │                     │                     │
        └─────────────────────┼─────────────────────┘
                              │  Document(title, parts, assets, warnings, status)
                       ┌──────▼───────┐
                       │  engine.py   │  assets/ writing, front matter, report,
                       │              │  output layout, batch walking
                       └──────┬───────┘
                              ▼
                        report.md + assets/  (+ report.md.json)
```

Five rules keep this from turning into a pile of special cases:

1. **A converter never writes files.** It returns a `Document`.
2. **A converter never guesses its own format.** The engine detected it.
3. **A failure is not fatal.** If the preferred converter raises, the next one
   in the chain runs and the fallback is recorded as a warning.
4. **A missing dependency degrades, it does not explode.** Register with
   `requires="openpyxl"` and the converter is skipped, with a note that a better
   engine existed.
5. **Every compromise is a warning.** `Document.warn()` downgrades the status to
   `partial`, which is what makes the batch summary trustworthy.

### Output conventions

Fixed, so downstream tools can rely on them:

* ATX headings (`##`), never setext — heading-based chunking works.
* GFM pipe tables, cells escaped, ragged rows padded.
* Fences sized to survive backticks in the content.
* Extracted images → `assets/` next to the output, content-addressed (identical
  bytes written once, name collisions disambiguated by hash).
* YAML front matter: source, format, converter, timestamp, fidelity, warnings.
* A conversion report appended between `<!-- tomd:report -->` markers — a
  machine-strippable appendix, or disable it with `--no-report`.
* Optional `<output>.md.json` sidecar for pipelines (`--report-json`).
* PDF pages wrapped in `<details>` so long documents stay navigable
  (`--page-markers` switches to HTML comments, useful for LLM input).

## Security

Converting a file means doing I/O with the privileges of the current process.
`tomd` treats that as a security boundary:

* uploads and archives are **size-capped and expansion-ratio capped** before
  anything is read (zip bombs are rejected, and the guard is tested),
* filenames from archives, uploads and document internals are sanitised before
  they touch the filesystem, and output paths are verified to stay inside the
  output tree,
* the batch walker skips `.git`, `node_modules`, virtualenvs and other noise,
* archives are **inventoried, not exploded** — auto-recursing is how one 10 KB
  upload becomes a 10 GB job,
* the web service binds to `127.0.0.1` only, refuses a non-loopback bind without
  an explicit `--allow-remote`, requires a per-process token in
  `X-Tomd-Token` (blocks cross-site form posts from a page you have open), and
  stages uploads in a scratch directory keyed by sanitised name,
* URL fetching is not implemented; when it is, `security.check_fetch_url()`
  already blocks non-HTTP schemes and private/loopback/metadata hosts (SSRF).

It is a **local** tool. Do not expose the server to a network without adding
authentication, quotas and a sandbox around the converters.

## Development

```bash
python scripts/make_fixtures.py      # regenerate the test corpus (27 files)
python -m pytest                     # 320+ tests
python -m pytest -q tests/test_pdf.py -k scanned
```

The test corpus is **generated, not committed**, so every fixture is readable
and its provenance is obvious — including the unhappy cases: a text-less
(scanned) PDF, a corrupt zip, a GBK-encoded file, a ragged TSV, a binary blob, a
`.txt` that is really JSON. Tests that need a fixture this Python cannot produce
are skipped, not failed.

```
tomd/
├── model.py          Document / Status / Warning / Asset — the IR
├── mdutil.py         markdown primitives, encoding-safe text reading
├── detect.py         format detection
├── registry.py       converter chains and degradation
├── security.py       sanitisation and limits
├── engine.py         orchestration, output layout, batch
├── cli.py            command line interface
├── server.py         local web service (stdlib http.server)
└── converters/
    ├── text.py       text, markdown, code, config, subtitles, diff
    ├── data.py       csv, json, jsonl, xml, notebooks
    ├── html.py       html (two engines), images
    ├── office.py     docx, pptx, xlsx, odf, rtf, legacy OLE
    ├── pdf.py        pypdf/pdfminer-free text+table+heading extraction
    ├── ebook.py      epub, fb2, eml, zip inventory
    └── unsupported.py explicit refusals
```

## Roadmap

Deliberately out of scope for this build, in rough order of value:

1. **OCR** for scanned PDFs and images (`tesseract` or a vision model). The
   detection and reporting are already in place — `scanned-pdf` names the exact
   pages that need it — so this is a converter, not a redesign.
2. **High-fidelity PDF layout** via Docling / Marker / MinerU as an opt-in
   engine behind the existing `pdf` chain (priority 5). They bring PyTorch-sized
   dependencies and often want a GPU, which is why they are not the default.
   Note that Marker's licence has commercial-use conditions worth checking.
3. **Audio/video transcription** (`faster-whisper`).
4. **Incremental batch mode** — a manifest of content hashes so re-running only
   converts what changed.
5. **`--strip-report` / chunk-friendly output profiles** for specific RAG stacks.

## Licence

MIT.

---

[中文文档](README.zh-CN.md)
