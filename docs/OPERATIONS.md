# Operations runbook

Run commands from the repository root.

## Bootstrap

```bash
./scripts/bootstrap.sh
.venv/bin/sansad-pipeline doctor
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/sansad-pipeline init
```

`doctor` reports required versions and optional engines; it makes no paid call.

## Census

Small smoke test:

```bash
.venv/bin/sansad-pipeline census-questions --limit-per-session 10
.venv/bin/sansad-pipeline census-rs-questions --limit-per-session 10
.venv/bin/sansad-pipeline census-elibrary-questions --limit 1000 --page-size 100
.venv/bin/sansad-pipeline census-status
```

Complete inventories:

```bash
.venv/bin/sansad-pipeline census-questions --all-available \
  --limit-per-session 0 --page-size 500 --workers 4
.venv/bin/sansad-pipeline census-rs-questions --all-available \
  --limit-per-session 0 --workers 4
.venv/bin/sansad-pipeline census-elibrary-questions --limit 0 \
  --page-size 100 --workers 8
```

The eLibrary hard limit is 100 items/page. `0` is explicitly unbounded. Export
with gzip and hash the snapshot:

```bash
.venv/bin/sansad-pipeline export-census data/exports/census-$(date +%F).jsonl.gz
sha256sum data/exports/census-*.jsonl.gz
```

Each discovery creates a run and checkpoint scopes. On `Ctrl-C`, active scopes
become `interrupted`; source errors become `failed` and the run `partial`.

```bash
# Retry failed current Lok Sabha session scopes
.venv/bin/sansad-pipeline census-questions --retry-failed-run RUN_ID \
  --limit-per-session 0 --page-size 250 --workers 2

# Retry failed/interrupted eLibrary pages
.venv/bin/sansad-pipeline census-elibrary-questions \
  --retry-failed-run RUN_ID --page-size 100 --workers 4
```

Rajya Sabha has no scoped retry option. Re-running discovery is safe because
records upsert by stable record ID.

## Storage planning and acquisition

```bash
.venv/bin/sansad-pipeline estimate-elibrary-storage --samples 100 --workers 4
df -h data
.venv/bin/sansad-pipeline acquire-census --source elibrary \
  --limit 1000 --workers 4 --min-free-gib 20
.venv/bin/sansad-pipeline census-status
```

`--source current` means current Lok Sabha plus Rajya Sabha; `all` also includes
eLibrary. `--lok-sabha` and `--session` narrow the queue. Normal acquisition
selects `discovered`; failed records require an explicit retry:

```bash
.venv/bin/sansad-pipeline acquire-census --source all \
  --limit 1000 --workers 4 --retry-failed
```

Free disk is checked during a batch. A low-disk stop returns non-success and
leaves remaining records resumable. Network downloads retry five times; file
admission verifies the PDF header, hashes the completed temporary file, and
atomically places it in the raw store.

## Direct ingest and extraction

```bash
.venv/bin/sansad-pipeline ingest path/to/file.pdf
.venv/bin/sansad-pipeline ingest-manifest samples/manifest.json
.venv/bin/sansad-pipeline download OFFICIAL_URL
.venv/bin/sansad-pipeline process SHA256_OR_UNAMBIGUOUS_PREFIX
.venv/bin/sansad-pipeline batch --workers 2
.venv/bin/sansad-pipeline status --json
```

An identical completed configuration reuses its run. `--force` creates a new
run. Document-level workers multiply `liteparse.workers`; two batch workers and
four internal workers can consume roughly eight concurrent OCR workers.

An interrupted parse may remain `running`. It is never reused/exported; a new
invocation creates another run. Retain its artifacts for diagnosis.

## Review and export

```bash
.venv/bin/sansad-pipeline compare-native SHA_PREFIX
.venv/bin/sansad-pipeline adjudicate SHA_PREFIX
.venv/bin/sansad-pipeline adjudicate SHA_PREFIX --pages 2,3
.venv/bin/sansad-pipeline export-jsonl data/exports/pages.jsonl
.venv/bin/sansad-pipeline export-jsonl data/exports/accepted.jsonl --accepted-only
.venv/bin/sansad-pipeline export-jsonl data/exports/canonical.jsonl \
  --accepted-only --with-adjudications
```

The last export includes locally accepted pages plus reviewed pages with an
adjudication, preserving local and paid provenance. “Canonical” does not imply
human verification or archival-grade numeric accuracy.

## Hugging Face publication

Authenticate once with `.venv/bin/hf auth login`. Build a tranche locally with
`prepare-publication`, inspect its `metadata.json`, and run
`verify-publication` before `publish-hf`. The upload command refuses a bundle
whose checksums do not validate. Remote verification should download the
published `SHA256SUMS` and compare every referenced object before local raw
files are considered safely replicated.

The WebDataset shard includes the original official PDF. A losslessly optimized
PDF is also included only when `qpdf --check` succeeds and the configured size
threshold is met. Never delete local originals merely because bundle creation
succeeded; publication and remote checksum verification are separate steps.

## OpenRouter safety

Copy `.env.example` to `.env` and add the key locally. Check model modality and
live pricing metadata without inference:

```bash
.venv/bin/sansad-pipeline check-openrouter-model \
  qwen/qwen3.7-flash qwen/qwen3.5-9b
```

Adjudication respects the configured page and cumulative reported-cost caps.
Authentication/payment errors stop immediately; retryable provider errors can
fall back. `--force` deliberately pays to repeat a completed page.

## Backup and recovery

A complete backup contains `pipeline.sqlite3`, `raw/`, and `artifacts/`.
`exports/` can be regenerated, though dated census exports are valuable
independent snapshots.

For a cold backup, stop all pipeline processes and snapshot the entire `data/`
tree. For a live backup, use SQLite's backup API or a consistent filesystem
snapshot. Do not copy only `pipeline.sqlite3` while its WAL is active.

Recovery:

1. Restore the complete `data/` tree at `storage.root`.
2. Run `status --json` and `census-status`.
3. Check several raw files against their filename hashes with `sha256sum`.
4. Run tests and process a known sample without force; its run should reuse.
5. Re-export census/canonical JSONL and compare counts.

Never repair state by editing raw PDFs. Test database surgery on a backup and
encode any lasting repair as a migration.

## Routine checklist

- Record date, commit, config copy, command, workers, disk, and exit code.
- Capture `doctor`, `status --json`, and `census-status`.
- Use bounded acquisition/processing tranches and inspect failures before retry.
- Hash and count published exports.
- Sample old scans, recent native PDFs, tables, decimals, dates, and ministries.
- Never place API keys in public logs.
