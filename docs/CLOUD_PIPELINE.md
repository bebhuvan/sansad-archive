# Cloud pipeline: GitHub Actions compute, Hugging Face storage

This is the fully online path for machines without disk space for the archive.
GitHub is compute only; Hugging Face is the system of record.

```text
GitHub Actions runner (ephemeral, 14 GB guaranteed SSD, 6 h/job)
  restore checkpoint from HF (immutable raw-PDF archive + mutable processing state)
  census session -> acquire originals -> extract -> Space Bunny adjudication
  optional MiMo cross-model verification
  checkpoint to HF after acquisition, every 100 extracted PDFs, and model batches
  build publication tranche -> verify SHA256SUMS -> upload to HF dataset repo
  verify remote file set and Git/LFS content hashes before marking published
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
| `source` | `current` API or the dated `elibrary` HF census snapshot |
| `house` | `lok_sabha` or `rajya_sabha` |
| `parliament` | Parliament number; blank for Rajya Sabha |
| `session` | Session number |
| `limit` | Documents to acquire; `0` means the whole session |
| `max_pages` | Model pages per adjudication chunk; `0` skips adjudication |
| `all_pages` | Adjudicate every page, not only flagged pages |
| `verify_sample` | Pages to cross-check with MiMo via OpenCode Go; `0` disables |
| `publish` | Build and upload the publication tranche |
| `tranche` | Tranche label; blank uses a content-derived snapshot label |

The nightly `schedule` runs the batch workflow; `PILOT_*` variables apply only
to a directly dispatched session workflow. GitHub disables scheduled
workflows after 60 days without repository activity; dispatch manually or keep
the repository active.

The session census and acquisition use the current Digital Sansad APIs, which
are session-scoped from 2000 (Lok Sabha) and 2001 (Rajya Sabha). Historical
eLibrary discovery is not session-scoped and remains a separate phase.
The Lok Sabha inventory comes from the official session endpoint, is deduplicated
and sorted numerically, and the batch starts with the newest sessions. Each
question census checks the API's reported row count across pages, rejects
missing pages and duplicate record IDs, and retains records with missing PDF
links as explicit acquisition failures. This prevents an incomplete crawl from
being mistaken for a successful empty session.

## Corpus scale and batch runs

| Inventory | House | Records | Dates | Cloud status |
|---|---:|---:|---|---|
| eLibrary Q&A | Lok Sabha | 1,158,768 (live count, 2026-09-24) | 1952-2026 | phase 3, not session-scoped |
| Current API | Lok Sabha | 179,089 | 2000-2026 | phase 2, 84 listed sessions (some empty/HTML-only) |
| Current RS API | Rajya Sabha | 258,987 | 2001-2026 | phase 2, 73 sessions |

Phase 1 is the bounded LS 18/8 pilot. Phase 2 is the **Digitize batch**
workflow: it lists every current-API session, skips scopes already marked
complete, and runs a bounded matrix of four scopes with at most two concurrent
sessions by default. Each nightly batch resumes the earliest unfinished scopes.
A scope is marked complete only when its census, acquisition, extraction,
all-page Space Bunny coverage, and publication are proven. A source record
whose official link is demonstrably HTML rather than a PDF is counted
separately and remains visible in the run summary; it cannot hide a transient
download failure. Markers live at
`state/complete/session-complete-<house>-p<parl>-s<session>.json`. The nightly
schedule re-runs the batch incrementally, so new sessions are picked up
automatically. The planner validates each existing HF completion or skip
marker against its recorded census, acquisition, extraction, model coverage,
and publication evidence. Older unproven marker filenames are ignored and
their scopes are revisited.

Scopes whose census returns zero records (typically pre-2000 sessions that the
current API lists but does not serve) are classified as empty: the workflow
skips publication, exits successfully, and uploads a skip marker to
`state/skipped/session-skipped-<key>.json`. The planner skips both complete and
skipped scopes, so empty sessions are attempted once, not nightly. The same
path covers legacy sessions whose records only link to `.htm` annexure pages
instead of PDFs, for example Lok Sabha 13/4, where all 31 records resolve to
`sansad.in/getFile/Annexture_New/...htm` with no PDF field. Those belong to the
historical eLibrary phase. Acquisition failures get one retry pass in the same
run. Transient failures remain eligible for the next batch; only a successful
zero-record census or an HTML-only scope receives a skip marker. To force a
re-attempt of an HTML-only scope, delete its marker from the dataset repository. A repository
dispatch input `exclude` (comma-separated `house:parliament:session`) can
temporarily omit a scope during a focused run. The normal batch does not
permanently exclude known server errors; failed scopes remain visible for retry.

Phase 3 is the historical eLibrary collection. It is not session-scoped, so it
needs a census slice imported into the runner (an `import-census` command) and
an explicit storage decision: the raw Lok Sabha Q&A originals alone are
projected at roughly 279 GiB. Ask datasets@huggingface.co for a storage grant
before starting, and expect weeks of wall-clock at batch parallelism.
The verified 2026-08-01 census export (1,593,344 records, SHA-256
`f0cec93b7f078af545a4b6a1647b50fdae4e90d769172b6bfd922481dbeb2b0e`)
is now durably stored on HF at
`state/census/snapshot-2026-08-01.jsonl.gz`, with a sibling JSON manifest.
The remote LFS SHA-256 and byte size match the local artifact. This is a dated
inventory, not a claim that its PDFs have been downloaded or that it includes
items added after 2026-08-01.
The `import-census` command can now verify that compressed snapshot hash and
stream a bounded eLibrary slice into an ephemeral runner's SQLite state. For
example, `sansad-pipeline import-census snapshot.jsonl.gz --source elibrary
--offset 0 --limit 1000 --sha256 <manifest SHA-256>`. Imported records start as
unacquired; the command does not falsely mark a historical session complete.
The session workflow also accepts `source=elibrary`: it restores a dedicated
checkpoint, downloads and verifies the HF snapshot, imports the requested
Lok Sabha parliament/session slice, resolves original PDF bitstreams, and
uses the same extraction, model, and publication stages. Its run summary
records the dated source. Even with `limit=0`, it does not claim a live
session-complete marker from a dated snapshot; historical coverage still
needs freshness reconciliation and a storage grant before bulk acquisition.
The eLibrary total changes as items are added. Its crawler validates the
returned page number, size, count, and item identifiers for each page; a
truncated or malformed response is a failed page, not a completed census.
`digitize-historical-batch.yml` plans Lok Sabha sessions from the verified
dated snapshot, running up to two scopes sequentially by default. It skips
only `state/snapshot-complete/` markers with the same census SHA-256, full
acquisition/extraction/model coverage, and a published tranche. These markers
do not assert that the live eLibrary has stopped changing; replacing the
snapshot invalidates them and resumes from existing PDF/model checkpoints.
The dated snapshot has both Roman and numeric session labels. One official
eLibrary item has a malformed label, `Anandgajapati RajuI`; the same official
metadata lists `Anandgajapati Raju` as a member. The crawler and snapshot
importer normalize only an exact listed-member prefix followed by a valid
session label, retaining the source string and rule in `raw.session_normalization`.
Unresolved labels are reported and quarantined by the planner, never guessed
from the question date.
The snapshot's eLibrary language metadata explicitly says `English` for
273,883 records and `Hindi` for seven; 881,378 say `Original` or have no tag.
Those are classified as `und` (undetermined), not silently marked English.
The source tag remains in raw provenance. Current LiteParse OCR is configured
for English; unknown/Hindi pages therefore keep both local and model layers
for comparison and require language-aware review before a canonical claim.

## Resume semantics

- The workflow restores `state/checkpoints/<scope>/checkpoint.json` before
  any work and saves after acquisition, every 100 extracted PDFs, and every second
  adjudication chunk. V2 checkpoints keep raw PDFs in a content-addressed
  archive and SQLite, extraction artifacts, and logs in a separate state
  archive. Unchanged originals are not re-uploaded for model-only checkpoints.
  The manifest and any new archives use one Hub commit; V1 single-archive
  checkpoints remain restorable.
- The checkpoint contains SQLite state, raw PDFs, extraction artifacts, and
  event logs. Rendered page PNGs are excluded because they are large and
  regenerable.
- Extraction selects PDFs lacking a complete run under the current LiteParse,
  routing, and validation configuration. Each batch is durably checkpointed
  before the next; even if one PDF fails, successful work is saved before the
  job fails. It stops at a deadline that leaves two hours for model work and
  publication, resuming from the checkpoint on the next run.
- Publication succeeds only after the remote HF tranche has exactly the local
  file set, matching sizes, and matching Git blob or LFS SHA-256 content IDs.
  A partial or altered upload cannot produce a completion marker.
- Adjudication runs in bounded chunks and skips pages that already have a
  stored adjudication for a configured model, so a re-run continues where the
  previous one stopped. Isolated page failures are retried in the next chunk;
  three consecutive chunks with no completed pages stop the run for diagnosis.
- If the provider returns a sustained 429, the chunk stops with
  `rate_limited: true` and the state is checkpointed. Publication waits for
  every acquired page to have a model layer, so incomplete passes do not
  create duplicate tranches. The next run resumes.
- Checkpoint artifact paths assume the same workspace path across runs, which
  holds for a repository with an unchanged name.

## Verified cloud canaries (2026-09-24)

- LS 17/15 run `36030891951` published 10 source PDFs and 34 pages. All 34
  pages had nonempty local and Space Bunny text. A fresh HF download contained
  exactly 50 files; all `SHA256SUMS` entries, original-PDF SHA-256s, and remote
  Git/LFS content IDs matched. This is a bounded tranche, not a complete
  1,499-record session.
- LS 17/14 run `36032323135` exercised V2 checkpoints and the automatic
  remote verifier. It published one PDF and two fully adjudicated pages. The
  raw archive SHA-256 was unchanged between extraction and model checkpoints;
  remote verification reported no missing or changed files.
- LS 17/15 run `36034109479` restored the real V1 checkpoint, migrated it to
  V2, reused all 34 stored model responses, and published a corrected 10-PDF
  tranche. Remote verification passed; visible-text numeric normalization
  reduced model comparison flags from seven to six without altering either
  source text layer.
- LS 17/14 continuation `36035410077` made new calls under the zero-cost
  ceiling. Its six published model pages each report `0.0` cost; the fatal-cost
  flag was false, and the two-PDF tranche passed remote verification.
- Historical eLibrary LS 01/I run `36036700034` published two original 1952
  PDFs and three pages under
  `data/lok_sabha/parliament-01/session-I/tranche-snapshot-local-9d0623763e05941fe35c`.
  Fresh HF reads verified both PDF SHA-256s, nonempty local and Space Bunny
  layers on every page, and `0.0` reported cost. This is a two-record canary,
  not a claim that the 2,950-record dated scope is complete. Those two source
  PDFs include one overlapping printed page; both originals remain preserved.
- Full LS 01/I historical continuation `36037513021` was dispatched at
  commit `f4d7c5a`, with acquisition and extraction checkpoints and free-only
  model enforcement. Its outcome is not yet verified here.
- LS 18/1 returned zero records despite appearing in the official session
  inventory, so its green skip run is not an extraction canary. The planner
  retains this distinction in the marker evidence.

## Accuracy policy

- Every page gets a local extraction and, by default in cloud runs, a Space
  Bunny Alpha transcription (`all_pages=true`, reasoning effort `low`,
  temperature 0). OCR still runs on every OCR-routed page, so scanned pages
  carry both an OCR candidate and a model candidate.
- LiteParse 2.14.7 supplies native extraction and built-in Tesseract OCR for
  pages whose native text is missing or suspect. The model adapter rejects
  nonzero or unknown provider pricing before making an inference call. If a
  response nonetheless reports a nonzero or unparseable charge, it writes a
  durable stop marker, cancels pending work, and refuses calls on resumed runs.
  The cumulative cost ceiling is zero. The
  optional MiMo verification stays disabled in the free-only overnight run.
- The layers stay separate in every record: `local_text`/`local_markdown`
  (native or OCR), `adjudicated_markdown` (Space Bunny), and the canonical
  `text`/`markdown`. The canonical policy defaults to `local`, so the model
  layer can be compared against the parser/OCR layer across the whole corpus
  before anyone promotes it; set `canonical_policy=model` to make the stored
  adjudication canonical.
- Model output is re-validated at publication time: empty output, replacement
  characters, inconsistent table widths, and numeric disagreement against the
  local candidate are recorded per page as `canonical_validation` and in the
  page tables. Numeric comparison counts Markdown link labels but not their
  destinations, so a URL rendered as `[visible URL](same URL)` is not double
  counted. Flags create review evidence, never silent corrections.
- A source-image check in the 10-document LS 17/15 canary found a PDF page
  whose visible table has an empty serial-number column while its selectable
  text layer contains row numbers. This is why image and native-text witnesses
  remain separate and a numeric disagreement is a review queue, not an
  automatic vote for either layer.
- All-pages adjudication is the throughput bottleneck: a large Lok Sabha
  session is roughly 15,000 pages, so one 6-hour job adjudicates only part of
  it. The loop is time-budgeted, checkpoints every second chunk, and a scope is
  marked complete only when acquisition and adjudication coverage are both
  finished, so multi-pass sessions resume instead of restarting.
- Cross-model verification (`scripts/verify_pages.py`) samples pages and
  compares an independent model (MiMo-V2.6-Flash on OpenCode Go) against the
  canonical text, reporting numeric disagreement, table consistency and text
  similarity. It never mutates stored output.
- Every model call stores the raw response, the exact request SHA-256, the
  model snapshot, token counts, reported cost, and timestamps under the run
  artifact directory.

## Provenance and formats

Each publication tranche contains:

- pilot tranches have `documents/<date>_<question>_<title>__<sha8>/` with
  `original.pdf`, `document.md`, `document.txt`, `document.json`;
- complete cloud sessions use compact WebDataset storage, keeping each
  original PDF and its text layers together under the full SHA-256 key. This
  avoids hundreds of thousands of small files in one HF repository;
- `manifest.jsonl` mapping every readable path or WebDataset key to its full SHA-256, official
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
