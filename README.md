# Sansad PDF pipeline

A local-first, English-only pipeline for acquiring, extracting, validating and
exporting Lok Sabha and Rajya Sabha PDFs. Raw PDFs are immutable and addressed
by SHA-256. Every derived page records the parser version, route, configuration,
confidence and validation flags.

The complete operator and maintainer handoff is indexed in
[`docs/README.md`](docs/README.md): architecture, configuration, database
semantics, source coverage, recovery, quality policy, and the explicit roadmap.
Dated experimental evidence remains in the reports under `results/`.

The pipeline is deliberately tiered:

```text
official PDF
    |
    +-- immutable content-addressed raw store
            |
            +-- LiteParse native extraction
                    |
                    +-- good native text ----------> accept
                    |
                    +-- empty/broken page ----------> OCR at 150 DPI
                    |
                    +-- full-page scan (even with an old text layer)
                                                   -> rasterize + fresh OCR at 250 DPI
                                                        |
                                                        +-- passes checks --> accept
                                                        +-- flagged -------> Paddle / NVIDIA review
```

`sparse-text` alone does not cause OCR. This matters because ruled Sansad table
pages frequently receive that label despite having a good native text layer.
Conversely, `full_page_image` always causes fresh OCR. The supplied text layer is
kept only as an independent comparison candidate; it is never accepted as the
new extraction merely because it contains many characters.

## Bootstrap

```bash
./scripts/bootstrap.sh
.venv/bin/sansad-pipeline doctor
```

The core environment pins LiteParse 2.14.7. `data/`, `.env`, optional engine
environments and virtual environments are ignored by Git.

## Acquire PDFs

Ingest local files without modifying them:

```bash
.venv/bin/sansad-pipeline ingest path/to/file.pdf another.pdf
```

Download official URLs:

```bash
.venv/bin/sansad-pipeline download 'https://sansad.in/path/document.pdf'
```

When a known checksum is available:

```bash
.venv/bin/sansad-pipeline download URL --expected-sha256 SHA256
```

Ingest the included provenance manifest:

```bash
.venv/bin/sansad-pipeline ingest-manifest samples/manifest.json
```

The store deduplicates identical bytes even when a document has several source
URLs. Sources remain separate database records.

## Census before mass download

Discovery and acquisition are separate. A census stores official URLs and
metadata in SQLite without downloading the PDFs:

```bash
# Current Digital Sansad API, latest session (10 records by default)
.venv/bin/sansad-pipeline census-questions

# All sessions exposed by that API
.venv/bin/sansad-pipeline census-questions --all-available \
  --limit-per-session 0 --page-size 500 --workers 4

# Retry only failed session scopes from a prior run
.venv/bin/sansad-pipeline census-questions --retry-failed-run RUN_ID \
  --limit-per-session 0 --page-size 250 --workers 2

# Rajya Sabha: latest or every session exposed by the official API
.venv/bin/sansad-pipeline census-rs-questions --limit-per-session 10
.venv/bin/sansad-pipeline census-rs-questions --all-available \
  --limit-per-session 0 --workers 4
```

The official Parliament eLibrary is the primary historical inventory. Its Lok
Sabha Questions and Answers collection reported 1,158,768 items on 2026-09-24;
the count can change as the collection grows:

```bash
# Inspect a small batch first
.venv/bin/sansad-pipeline census-elibrary-questions --limit 1000

# Stream the whole collection into SQLite (0 means unlimited)
.venv/bin/sansad-pipeline census-elibrary-questions --limit 0 \
  --page-size 100 --workers 8
```

The eLibrary caps API pages at 100 items; the CLI rejects larger values to
prevent silent pagination gaps. Every page is checkpointed and failed pages can
be retried with `--retry-failed-run RUN_ID`.

The completed 2026-08-01 census contains 1,593,344 source records: 1,155,268
Lok Sabha eLibrary items, 179,089 current-API Lok Sabha records, and 258,987
current-API Rajya Sabha records. See
[`results/census/REPORT.md`](results/census/REPORT.md) for coverage gaps,
storage sizing, and historical OCR findings.

The eLibrary connector reads item metadata and resolves the `ORIGINAL` PDF
bitstream only at acquisition time. It deliberately ignores any supplied OCR or
digitized text. Download a bounded tranche, inspect status, and export the
inventory with:

```bash
.venv/bin/sansad-pipeline acquire-census --source elibrary --limit 100 --workers 4
.venv/bin/sansad-pipeline census-status
.venv/bin/sansad-pipeline export-census data/exports/census.jsonl.gz
```

Use a `.gz` suffix for streaming gzip output; plain `.jsonl` remains supported.

Acquisition is resumable and batched; `--limit 0` means all pending records.
The command stops before free space falls below 10 GiB by default (override with
`--min-free-gib`) and failed records are retried only when `--retry-failed` is
explicitly supplied.

Before mass acquisition, sample original bitstream sizes across the collection:

```bash
.venv/bin/sansad-pipeline estimate-elibrary-storage --samples 20 --workers 4
```

The first 12-point distributed sample projected roughly 279 GiB of raw Lok
Sabha Q&A PDFs, while the development machine had about 61 GiB free. Treat that
as a planning estimate and increase the sample before provisioning object
storage; the pipeline does not begin an unbounded download automatically.

## Publish research tranches

Publication is deliberately bounded by House, Parliament and session. A bundle
contains original PDFs and optional losslessly optimized PDF derivatives in a
WebDataset TAR, plus per-document Markdown, JSON and text, bulk Zstandard JSONL,
Parquet tables, a DuckDB snapshot and `SHA256SUMS`:

```bash
.venv/bin/sansad-pipeline prepare-publication \
  --house lok_sabha --parliament 18 --session 8 --limit 10 \
  --output data/publications/ls18-session8-pilot
.venv/bin/sansad-pipeline verify-publication \
  data/publications/ls18-session8-pilot
.venv/bin/sansad-pipeline publish-hf \
  data/publications/ls18-session8-pilot \
  --repo USER/sansad-corpus \
  --path-in-repo data/lok_sabha/parliament-18/session-8/pilot-0001
```

PDF extraction always uses the immutable official original. `qpdf` compression
is lossless, validated, and retained only when it saves at least 5% by default.
Publication never claims a complete session merely because a bounded tranche
was uploaded.

Several metadata rows may legitimately resolve to the same PDF. Those records
remain distinct in the census while the immutable raw store deduplicates the
actual bytes by SHA-256.

## Process

Use a full SHA-256 or an unambiguous prefix:

```bash
.venv/bin/sansad-pipeline process 6fdb18223ad4
```

Process everything resumably:

```bash
.venv/bin/sansad-pipeline batch --workers 2
```

Process one publication scope without touching unrelated documents:

```bash
.venv/bin/sansad-pipeline process-scope \
  --house lok_sabha --parliament 18 --session 8 --workers 2
```

Document workers multiply LiteParse's internal OCR workers, so keep the product
within the machine's CPU/RAM capacity (the default document worker count is 1).

Successful runs with the identical configuration are reused. Changed
configuration creates a new run. `--force` always creates a new run.

Outputs live under:

```text
data/
  raw/sha256/ab/<sha256>.pdf
  pipeline.sqlite3
  artifacts/<sha256>/run-00000001/
    manifest.json
    document.json
    document.md
    page-00001.json
  exports/
```

Inspect corpus state and export the latest successful page versions:

```bash
.venv/bin/sansad-pipeline status
.venv/bin/sansad-pipeline export-jsonl data/exports/pages.jsonl
.venv/bin/sansad-pipeline export-jsonl data/exports/accepted.jsonl --accepted-only
.venv/bin/sansad-pipeline export-jsonl data/exports/canonical.jsonl \
  --accepted-only --with-adjudications
```

The canonical export uses local output for accepted pages and the latest stored
OpenRouter transcription for reviewed pages that have one. It retains the local
candidate, model, token counts, reported cost and response path in each record.

## Validation

Current automatic checks flag:

- empty output;
- Unicode replacement characters;
- low OCR confidence;
- inconsistent Markdown table widths;
- numeric disagreement when both native and OCR text exist.

Flags create `review` status; they never rewrite the source or silently invent a
correction. The validation layer is intentionally conservative and can be
expanded with Sansad-specific question/date/ministry/table schemas.

## NVIDIA Nemotron vision review

Nemotron is the primary page transcription branch for the session corpus. It
receives every page as a 150-DPI image, keeps the local candidate as an
independent comparison, and stores the model response separately:

```bash
# One document, every page (up to the configured per-command ceiling)
.venv/bin/sansad-pipeline adjudicate-nvidia DOCUMENT_SHA --all-pages

# One House/session scope, sequential and resumable
.venv/bin/sansad-pipeline adjudicate-scope-nvidia \
  --house rajya_sabha --parliament '' --session 271
```

The scope command always processes every page. Set `NVIDIA_API_KEY` in ignored
`.env`. HTTP 429 and transient 5xx responses use
bounded exponential backoff with jitter and `Retry-After`; socket timeouts are
also recorded and retried once. Every attempt and accepted response has durable
provenance. See the dated
[`one-session-per-House pilot`](results/session_pilot/REPORT.md).

## pdf-inspector second opinion

The latest tested source tag is `v0.7.0`. Build it into the core environment:

```bash
./scripts/install_pdf_inspector.sh
.venv/bin/sansad-pipeline compare-native DOCUMENT_SHA
```

The command stores its Markdown and a numeric-token comparison alongside the
LiteParse run. Agreement is evidence, not ground truth.

## PaddleOCR fallback

LiteParse accepts external OCR servers. The supplied setup uses the PaddleOCR
wrapper shipped by the pinned LiteParse repository:

```bash
./scripts/setup_paddleocr.sh
./scripts/start_paddleocr.sh
```

Then set this in `pipeline.toml` and create a new run:

```toml
[liteparse]
ocr_server_url = "http://127.0.0.1:8829/ocr"
```

```bash
.venv/bin/sansad-pipeline process DOCUMENT_SHA --force
```

PaddleOCR is isolated under `.engines/`; it does not make the core installation
large or fragile. The first run downloads its models.

## Space Bunny Alpha adjudication (OpenRouter)

Space Bunny Alpha is the default vision model: a free stealth model on
OpenRouter with image input, a 1M context and mandatory reasoning. The pipeline
sets `reasoning_effort = "low"`, temperature 0, retries 429/5xx with backoff
and jitter, honors `Retry-After`, and stores the raw response plus the exact
request SHA-256 for every page.

1. Add the key to [.env](.env):

   ```dotenv
   OPENROUTER_API_KEY=your-key
   ```

2. `pipeline.toml` already configures the model:

   ```toml
   [openrouter]
   model = "stealth/space-bunny-alpha"
   models = ["stealth/space-bunny-alpha"]
   reasoning_effort = "low"
   ```

3. Adjudicate flagged pages for one document:

   ```bash
   .venv/bin/sansad-pipeline adjudicate DOCUMENT_SHA
   ```

   Every page, or one scope, resumably in bounded chunks:

   ```bash
   .venv/bin/sansad-pipeline adjudicate DOCUMENT_SHA --all-pages
   .venv/bin/sansad-pipeline adjudicate-scope-openrouter \
     --house lok_sabha --parliament 18 --session 8 \
     --limit-pages 500 --workers 3 --summary-out /tmp/summary.json \
     --log data/logs/adjudication.jsonl
   ```

The scope command skips pages that already have a stored adjudication for a
configured model. When every configured model is rate limited it stops with
`rate_limited: true` instead of hammering the provider; re-run to continue.
Flags create review status; model output never overwrites the local extraction
automatically. Each attempt uses a unique artifact directory, and model,
request hash, tokens and cost are recorded in SQLite. Inspect a model without
inference:

```bash
.venv/bin/sansad-pipeline check-openrouter-model stealth/space-bunny-alpha
```

## Cross-model verification

`scripts/verify_pages.py` samples extracted pages and sends them to an
independent model (default MiMo-V2.6-Flash on OpenCode Go) and reports numeric
disagreement, table consistency, and text similarity against the canonical
text. It is evidence, never a correction. Set `OPENCODE_API_KEY`, or rely on the
local OpenCode auth file, and run:

```bash
.venv/bin/python scripts/verify_pages.py \
  --house lok_sabha --parliament 18 --session 8 --sample 50
```

## Fully online runs

For machines without local disk space, `.github/workflows/digitize-session.yml`
runs census, acquisition, extraction, adjudication, publication and upload on a
GitHub Actions runner while Hugging Face holds the corpus and checkpoints.
`.github/workflows/digitize-batch.yml` queues every current-API session as a
resumable matrix and skips scopes already marked complete, so its nightly
schedule works as an incremental scheduler. See
[docs/CLOUD_PIPELINE.md](docs/CLOUD_PIPELINE.md) for the corpus map,
prerequisites, resume semantics, and limits.

## Tests and benchmark

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/benchmark_liteparse.py --resume
```

The initial empirical findings are in [REPORT.md](REPORT.md), with raw benchmark
results in `results/liteparse_2.10.1/`.

The first paid Qwen vision comparison is in
[`results/openrouter_qwen_trial/REPORT.md`](results/openrouter_qwen_trial/REPORT.md).
