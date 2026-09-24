# Cloud pipeline: GitHub Actions compute, Hugging Face storage

This is the fully online path for machines without disk space for the archive.
GitHub is compute only; Hugging Face is the system of record.

```text
GitHub Actions runner (ephemeral, 14 GB guaranteed SSD, 6 h/job)
  restore checkpoint from HF
  census session -> acquire originals -> extract -> Space Bunny adjudication
  optional MiMo cross-model verification
  checkpoint to HF after every stage
  build publication tranche -> verify SHA256SUMS -> upload to HF dataset repo
```

Nothing is committed to Git. Raw PDFs and derived artifacts live in the
Hugging Face dataset repository under `data/<house>/parliament-<n>/session-<s>/`.
A checkpoint under `state/checkpoints/...` makes an interrupted run resumable
without repeating model calls.

## Why this split

- GitHub repositories are not storage: 100 MB per-file rejections, roughly
  1 GB recommended repository size, metered LFS, and bulk data hosting violates
  the terms. Repositories hold code, tests, and workflows.
- Standard GitHub-hosted runners are free and unlimited for public
  repositories: 4 vCPU, 16 GB RAM, 14 GB guaranteed free disk, 6 hours per job.
  Free artifact storage is only 500 MB, so the corpus never uses Actions
  artifacts.
- Hugging Face public datasets are free and best-effort; private storage is
  100 GB free, PRO is 10 TB public plus 1 TB private. Keep the dataset public,
  shard it per session/tranche, and email datasets@huggingface.co for a storage
  grant if the corpus outgrows the free tier. Keep repositories under 100k
  files and folders under 10k entries; the bundle's WebDataset, Parquet and
  JSONL files are already shaped for this.

## Prerequisites

1. A public GitHub repository containing this project.
2. Repository secrets:
   - `OPENROUTER_API_KEY` for Space Bunny Alpha (required);
   - `HF_TOKEN` with write access to the dataset repository (required);
   - `OPENCODE_API_KEY` for MiMo cross-model verification (optional).
3. Repository variable `HF_DATASET_REPO`, for example `your-user/sansad-corpus`.
4. Optional repository variables for the nightly schedule:
   `PILOT_HOUSE`, `PILOT_PARLIAMENT`, `PILOT_SESSION`, `PILOT_LIMIT`,
   `PILOT_MAX_PAGES`.

Never put keys in workflow files, repository files, or command lines; only in
repository secrets.

## Running

Use **Actions -> Digitize session -> Run workflow**:

| Input | Meaning |
|---|---|
| `house` | `lok_sabha` or `rajya_sabha` |
| `parliament` | Parliament number; blank for Rajya Sabha |
| `session` | Session number |
| `limit` | Documents to acquire; `0` means the whole session |
| `max_pages` | Model pages per adjudication chunk; `0` skips adjudication |
| `all_pages` | Adjudicate every page, not only flagged pages |
| `verify_sample` | Pages to cross-check with MiMo via OpenCode Go; `0` disables |
| `publish` | Build and upload the publication tranche |
| `tranche` | Tranche label; blank uses timestamp plus run id |

The nightly `schedule` uses the `PILOT_*` variables. GitHub disables scheduled
workflows after 60 days without repository activity; dispatch manually or keep
the repository active.

The session census and acquisition use the current Digital Sansad APIs, which
are session-scoped from 2000 (Lok Sabha) and 2001 (Rajya Sabha). Historical
eLibrary discovery is not session-scoped and remains a separate phase.

## Corpus scale and batch runs

| Inventory | House | Records | Dates | Cloud status |
|---|---:|---:|---|---|
| eLibrary Q&A | Lok Sabha | 1,155,268 | 1952-2026 | phase 3, not session-scoped |
| Current API | Lok Sabha | 179,089 | 2000-2026 | phase 2, 76 sessions |
| Current RS API | Rajya Sabha | 258,987 | 2001-2026 | phase 2, 73 sessions |

Phase 1 is the bounded LS 18/8 pilot. Phase 2 is the **Digitize batch**
workflow: it lists every current-API session, skips scopes already marked
complete, and runs them as a matrix with `max_parallel` concurrent sessions
(default 3). A scope is marked complete only when every discovered record is
acquired and none failed; markers live at
`state/complete/session-complete-<house>-p<parl>-s<session>.json`. The nightly
schedule re-runs the batch incrementally, so new sessions are picked up
automatically.

Phase 3 is the historical eLibrary collection. It is not session-scoped, so it
needs a census slice imported into the runner (an `import-census` command) and
an explicit storage decision: the raw Lok Sabha Q&A originals alone are
projected at roughly 279 GiB. Ask datasets@huggingface.co for a storage grant
before starting, and expect weeks of wall-clock at batch parallelism.

## Resume semantics

- The workflow restores `state/checkpoints/<scope>/checkpoint.tar.zst` before
  any work and saves after acquisition and after every adjudication chunk.
- The checkpoint contains SQLite state, raw PDFs, extraction artifacts, and
  event logs. Rendered page PNGs are excluded because they are large and
  regenerable.
- Adjudication runs in bounded chunks and skips pages that already have a
  stored adjudication for a configured model, so a re-run continues where the
  previous one stopped.
- If the provider rate-limits every configured model, the chunk stops with
  `rate_limited: true`, the state is checkpointed, and the run still publishes
  whatever completed. The next run resumes.
- Checkpoint artifact paths assume the same workspace path across runs, which
  holds for a repository with an unchanged name.

## Accuracy policy

- Local LiteParse extraction is the baseline for every page. Space Bunny Alpha
  (a vision model, reasoning effort `low`, temperature 0) adjudicates flagged
  pages by default; `all_pages` is for experiments, because a model can only
  degrade a byte-exact native text layer.
- Canonical text is selected deterministically: the stored adjudication when
  one exists, otherwise the local extraction. Both candidates are always
  retained as `local_markdown` and `markdown`.
- Model output is re-validated at publication time: empty output, replacement
  characters, inconsistent table widths, and numeric disagreement against the
  local candidate are recorded per page as `canonical_validation` and in the
  page tables. Flags create review evidence, never silent corrections.
- Cross-model verification (`scripts/verify_pages.py`) samples pages and
  compares an independent model (MiMo-V2.6-Flash on OpenCode Go) against the
  canonical text, reporting numeric disagreement, table consistency and text
  similarity. It never mutates stored output.
- Every model call stores the raw response, the exact request SHA-256, the
  model snapshot, token counts, reported cost, and timestamps under the run
  artifact directory.

## Provenance and formats

Each publication tranche contains:

- `documents/<date>_<question>_<title>__<sha8>/` with `original.pdf`,
  `document.md`, `document.txt`, `document.json`;
- `manifest.jsonl` mapping every readable path to its full SHA-256, official
  source URLs, record IDs, house/parliament/session, date, question number,
  title, ministry and page count;
- `pages.parquet`, `documents.parquet`, `pages.jsonl.zst`,
  `documents.jsonl.zst`, `sansad.duckdb` for analysis;
- `webdataset/shard-00000.tar` keyed by SHA-256 for streaming and training;
- `metadata.json` with the pipeline commit, counts, and optimization records;
- `SHA256SUMS` for integrity.

JSON is the machine truth, Markdown is for reading and citation, plain text is
for full-text search and NLP, and the columnar formats are for corpus analysis.

## Limits and risks

- Space Bunny Alpha is a stealth model: free, temporary, and not guaranteed to
  exist. Its rate limits are not published and the stealth terms require
  reasonable request volume. The pipeline retries with exponential backoff and
  jitter, honors `Retry-After`, and stops the chunk when every model is rate
  limited.
- Fallback capacity: OpenCode Go serves MiMo-V2.6-Flash, Muse Spark 1.3
  Contributor, and Space Bunny Free under a subscription with monthly usage
  limits; NVIDIA Nemotron remains a free review branch. Muse Spark Contributor
  tiers may use prompts for training; official Parliament PDFs are public, but
  treat that as a policy decision.
- A job is capped at 6 hours and 14 GB. One session is roughly 1 GB of raw
  PDFs, so shard by session or by `limit` for larger scopes.
- The dataset card and tranche README state that publications are bounded
  tranches, not complete sessions, and that machine text may contain errors.
