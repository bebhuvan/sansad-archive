# Command reference

Global form:

```text
sansad-pipeline [--config PATH] COMMAND [OPTIONS]
```

`--config` defaults to the repository `pipeline.toml` and must occur before the
command. Use `sansad-pipeline COMMAND --help` for argparse's live reference.

| Command | Arguments and options | Result |
|---|---|---|
| `init` | none | Create storage directories and initialize/migrate SQLite |
| `doctor` | none | Report required versions and optional tools |
| `ingest` | `PDF [PDF ...]` | Verify and copy local PDFs into immutable storage |
| `ingest-manifest` | `MANIFEST` | Ingest local/downloadable JSON manifest entries |
| `download` | `URL [URL ...]`, `--expected-sha256 SHA` for one URL | Download, verify, hash, and store official PDFs |
| `process` | `SHA_OR_PREFIX [..]`, `--force` | Extract specified documents; prefixes must be unambiguous |
| `batch` | `--force`, `--limit N`, `--workers N` | Resumably extract ingested documents |
| `status` | `--json` | Count documents, sources, runs, routes, review pages, cost |
| `scope-status` | `--house`, `--parliament`, `--session` | Scope-specific acquisition, bytes, pages, OCR, review and Nemotron counts |
| `export-jsonl` | `OUTPUT`, `--accepted-only`, `--with-adjudications` | Export latest completed page runs |
| `compare-native` | `SHA_OR_PREFIX` | Store a pdf-inspector comparison |
| `adjudicate` | `SHA_OR_PREFIX`, `--pages 1,2`, `--model ID`, `--all-pages`, `--include-ocr`, `--force` | Vision review; default pages are locally flagged |
| `adjudicate-scope-openrouter` | `--house`, `--parliament`, `--session`, `--limit-pages N`, `--workers N`, `--all-pages`, `--include-ocr`, `--force`, `--summary-out PATH`, `--log PATH` | Resumable OpenRouter adjudication of one scope; `--include-ocr` adds every scanned page; stops on sustained 429 |
| `adjudicate-nvidia` | `SHA_OR_PREFIX`, `--pages 1,2`, `--all-pages`, `--force` | Throttled Nemotron transcription; default is flagged pages unless `--all-pages` |
| `process-scope` | `--house`, `--parliament`, `--session`, `--workers` | Process only one publication scope resumably |
| `adjudicate-scope-nvidia` | `--house`, `--parliament`, `--session`, `--limit-pages`, `--workers` | Concurrent, resumable Nemotron transcription of every page in one scope |
| `prepare-publication` | `--house`, `--parliament`, `--session`, `--output`, `--canonical-policy local\|model` | Build PDF/MD/JSONL/Parquet/DuckDB/WebDataset tranche with checksums; both text layers are always retained |
| `verify-publication` | `BUNDLE` | Verify every publication file against `SHA256SUMS` |
| `publish-hf` | `BUNDLE`, `--repo`, `--path-in-repo` | Upload a verified tranche to a Hugging Face dataset |
| `check-openrouter-model` | `MODEL [MODEL ...]` | Check live modality/pricing metadata without inference |
| `census-questions` | `--lok-sabha N`, `--session N`, `--all-available`, `--limit-per-session N`, `--page-size N`, `--sleep SECONDS`, `--workers N`, `--retry-failed-run ID` | Discover current Lok Sabha English Q&A records |
| `census-rs-questions` | `--session N`, `--all-available`, `--limit-per-session N`, `--workers N` | Discover current Rajya Sabha English Q&A records |
| `census-elibrary-questions` | `--limit N`, `--page-size 1..100`, `--start-page N`, `--workers N`, `--retry-failed-run ID` | Discover historical Lok Sabha eLibrary Q&A records |
| `acquire-census` | `--lok-sabha N`, `--session N`, `--limit N`, `--source all/current/elibrary`, `--workers N`, `--retry-failed`, `--min-free-gib N` | Download pending census PDFs |
| `census-status` | none | Group acquisition state and show recent census runs/scopes |
| `estimate-elibrary-storage` | `--samples N`, `--workers N` | Sample ORIGINAL sizes and project raw storage |
| `export-census` | `OUTPUT.jsonl[.gz]` | Stream the complete census inventory |

## Defaults that affect safety

- `batch --workers`: 1; optional `--limit` has no zero-is-all promise.
- Lok Sabha census: 10 records/session, page size 100, sleep 0.25 s, 4 workers.
- Rajya Sabha census: 10 records/session, 4 workers.
- eLibrary census: 1,000 items, page size 100, page 0, 4 workers.
- Acquisition: 10 records, all sources, 2 workers, 10 GiB free-space floor.
- Storage estimate: 20 samples, 4 workers.
- Census/acquisition limit values of `0` mean all available/pending records.
- Adjudication defaults to review pages and skips already successful pages.

Commands return nonzero for processing failures, failed discovery scopes/pages,
failed/low-disk acquisition, rejected model checks, failed adjudications, or
failed storage samples. Discovery can still persist useful partial results when
its command exits nonzero.
