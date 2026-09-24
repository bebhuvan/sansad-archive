# Architecture

## Objective and boundary

The project builds a fresh, English-only archive from official Parliament PDFs.
It does not trust a government-supplied OCR layer as ground truth. Original PDF
bytes, source metadata, local extraction, validation, and optional paid
adjudication remain separately recoverable.

The implemented source scope is Lok Sabha and Rajya Sabha questions and answers.
Debates, proceedings, and standing committee transcripts are future connectors.

## Data flow

```text
official APIs / eLibrary
          |
          v
census metadata in SQLite ----------------------> census JSONL(.gz)
          |
          v
download ORIGINAL PDF
          |
          v
immutable SHA-256 raw store <---- local PDF / manifest / direct URL
          |
          v
LiteParse native pass and page inspection
          |
          +-- reliable native page -------------------------+
          +-- empty/broken -> ordinary OCR at 150 DPI       |
          +-- full-page scan -> image-only PDF, OCR 250 DPI |
                                                            v
                                                      validation
                                                     /          \
                                             accepted            review
                                                                  |
                                      pdf-inspector / Paddle / NVIDIA
                                                                  |
                                                                  v
                                                        canonical JSONL
```

Discovery never downloads a PDF. Acquisition never parses it. Parsing never
silently promotes a paid response. Each expensive stage is bounded and
resumable.

## Trust and immutability rules

1. Admitted files must begin with `%PDF-` and are hashed before storage.
2. Raw files live at `data/raw/sha256/<prefix>/<sha256>.pdf`, are read-only, and
   deduplicate by bytes rather than URL.
3. Multiple official records or URLs may point to one hash; provenance remains
   in separate source/census records.
4. Every extraction run snapshots LiteParse, routing, and validation config.
5. Page artifacts are append-only per run. Changed config or `--force` creates a
   new run directory.
6. Remote model output is an adjudication beside local output. It never overwrites
   a page artifact or raw PDF.
7. Exports select the latest completed run, never a failed/incomplete one.

## Extraction and review

All pages receive a native LiteParse pass. Empty/short/garbled pages and selected
complexity reasons route to OCR. `sparse-text` alone does not: parliamentary
tables can trigger it while retaining good native text.

For an archival scan containing a dense old OCR layer, native extraction is not
independent digitization. A detected full-page image is rendered into an
in-memory image-only PDF and freshly OCRed at 250 DPI. The old layer remains
only as a comparison candidate.

Empty output, replacement characters, low OCR confidence, inconsistent Markdown
table widths, or native/OCR numeric disagreement marks a page `review`. No
validator guesses the correct number.

NVIDIA Nemotron is review-only, sequentially throttled, and persists retry and
rate-limit evidence. OpenRouter remains available for controlled comparisons.
It has ordered model fallback, page and cumulative-cost ceilings, and an
English-only prompt that omits rather than translates Hindi.

## Component map

| Path | Responsibility |
|---|---|
| `cli.py` | CLI and argument validation |
| `config.py` | Typed TOML configuration |
| `db.py` | SQLite schema and migration |
| `storage.py` | Download, hashing, immutable raw storage |
| `census.py` | Discovery checkpoints, acquisition, census export |
| `sources/questions.py` | Current Lok Sabha and Rajya Sabha connectors |
| `sources/elibrary.py` | eLibrary discovery and ORIGINAL resolution |
| `liteparse_engine.py` | LiteParse adapter and forced image-only OCR |
| `routing.py` / `validation.py` | Page selection and quality flags |
| `nvidia.py` | Throttled Nemotron vision adjudication and retry provenance |
| `openrouter.py` | Optional paid vision adjudication and fallback |
| `secondary.py` | pdf-inspector comparison |
| `scripts/` / `tests/` | Setup, benchmarks, optional engines, and tests |

All Python paths above are under `sansad_pipeline/`.

## On-disk layout

```text
data/
  pipeline.sqlite3
  raw/sha256/ab/<sha256>.pdf
  artifacts/<sha256>/run-00000001/
    manifest.json
    document.json
    document.md
    page-00001.json
    openrouter/...
  exports/
  tmp/
```

SQLite is the state machine; raw PDFs and artifacts are the evidence. A complete
recovery needs both.
