# Cloud pipeline: GitHub Actions compute, Hugging Face storage

This is the fully online path for machines without disk space for the archive.
GitHub is compute only; Hugging Face is the system of record.

```text
GitHub Actions runner (ephemeral, 14 GB guaranteed SSD, 6 h/job)
  restore checkpoint from HF (immutable raw-PDF shards + mutable processing state)
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
Acquisition saves after each 250-record chunk by default. The former
1,000-record chunk left LS 01/III with a checkpoint gap exceeding 36 minutes
while the old job was still actively acquiring; a runner loss in that window
would have repeated those downloads. Smaller chunks trade a few more HF state
commits for a shorter uncheckpointed interval. A chunk already running on an
older commit is not changed or cancelled by this default.
At 00:09 UTC on 2026-09-25, LS 01/III was still live in the older run's
acquisition step, with its last HF checkpoint at 23:29 UTC and 2,000 of
3,677 records downloaded. A read-only probe of the next listed eLibrary
item's official ORIGINAL bundle returned in 0.56 seconds; that does not
prove the runner is making progress, so the job remains under monitoring.
The older pending historical batch `36074837899` was replaced by pending
batch `36076235170` on commit `e6495fe`, carrying the 250-item acquisition
checkpoint default. The active batch and its runner were not cancelled.
At 00:16 UTC, a fresh HF read finally exposed that active job's next
checkpoint, created at 00:07 UTC: 3,000 of 3,677 items downloaded, 1,902
distinct selected PDF bytes retained, and 677 still discovered. The job
remained live in acquisition. The older checkpoint observed at 00:13 UTC
was therefore an incomplete observation of a chunk still becoming durable,
not proof of a stalled runner. The interval from one visible remote checkpoint
to the next was about 47 minutes; future 250-item chunks reduce that exposure.

The published WebDataset shard stores each selected official PDF as
`<sha256>.original.pdf` beside its local, OCR/model, and canonical text layers.
Publication verification rehashes the embedded PDF and requires its SHA-256 to
match the manifest key; the remote verifier checks uploaded file identities.
The HF checkpoint also retains immutable raw-PDF shards for resumability.
The on-Hub checkpoint manifest keeps the full per-PDF hash index. The Actions
save command prints only its counts, archive size, shard count and commit URL;
dumping thousands of raw-index entries into each model-chunk log made failure
triage unnecessarily noisy.
LiteParse 2.14.7's process pool now enforces a 300-second hard limit on each
native or OCR parse call. Its worker is closed after success or error. A rogue
parse is recorded as a failed document and cannot hold an extraction chunk
indefinitely in that parser call; the next resumable pass can retry it. A local
integration test ran the pool inside the same spawned-process shape used by
cloud extraction.
This safeguard does not impose a hard timeout on the separate screenshot step
used for full-page-image OCR routing.
At 00:18 UTC on 2026-09-25, GitHub had not emitted the 00:17 scheduled
current-API batch. The older pending scheduled batch `36071728804` was
cancelled before starting and replacement batch `36076943330` queued on
commit `a9ea4af`, with all-pages Space Bunny, OCR, local canonical, four
scopes, two runners and completion-marker skipping explicitly set. It remains
pending behind the active modern batch, whose LS 18/5 and 18/6 jobs were both
in model adjudication; neither active job was interrupted.

**eLibrary attachment completeness is a separate question from item coverage.**
At 22:52 UTC on 2026-09-24, live item
`49d60eef-b8d0-4c19-83bf-2804f0a8c3d0` (LS 17/IX) exposed two ORIGINAL
bitstreams, `AU3055.pdf` and `AU3055_hindi.pdf`. The acquisition path from
commit `ea3186f` onward intended English-first processing but selected only
the first listed PDF, regardless of its language label. The
subsequent acquisition change downloads every PDF in the ORIGINAL bundle into
the immutable raw checkpoint, records each bitstream-to-SHA mapping in SQLite,
and leaves the item retryable if an attachment fails. One selected PDF enters
the present text pipeline; Hindi extraction remains a later phase. The listing
rejects truncation rather than silently treating the first page as complete.
It also requires a complete bundle-page count and exactly one `ORIGINAL`
bundle; ambiguous source inventory fails acquisition instead of silently
omitting PDFs. A live two-PDF eLibrary item still resolves both originals
with this stricter check, and the full test suite passes.
New acquisitions prefer an explicitly English-named original, then the first
PDF without an explicit Hindi label, while preserving official bitstream order
in the full attachment ledger. An unlabeled bundle remains ambiguous and is
not asserted to be English. A reversed Hindi-first two-PDF test verifies that
the English PDF becomes the selected text source and both originals survive.
This rule applies to new acquisitions; already-published historical tranches
are not retroactively relabelled. On replay, the acquisition stage checks the
retained attachment ledger and reselects only when there is an explicitly
English alternative or the old selected filename is explicitly Hindi. The
old raw bytes and text runs remain available; the new selected PDF must pass
through extraction and Space Bunny before a new tranche can be complete.
Ambiguous names are counted, left unchanged and not asserted to be English.
On a new-code continuation, the acquisition step revisits older downloaded
items without an attachment ledger, reuses a selected PDF only when its source
URL matches, downloads the other PDFs, and checkpoints every 100 completed
items. It exits nonzero while any downloaded item still lacks a complete
ledger, so extraction/publication cannot advance through an unresolved
attachment backfill. Publication now includes every ledger PDF in the same
WebDataset shard as the selected PDF and text, with per-bitstream source URL,
name and SHA-256 in `manifest.jsonl` and document JSON. The verifier rehashes
each attachment. The resolver also checks the official bitstream format link
when a filename lacks `.pdf`, so an extensionless `application/pdf` attachment
is included. Existing older publications and complete markers remain
selected-PDF-only evidence until their scopes are replayed and republished;
do not delete or replace them based solely on this discovery. The first full
cloud backfill and remote publication proof are recorded below; it does not
establish attachment completeness for every older scope.

The first full historical backfill finished in GitHub run
[`36072353217`](https://github.com/bebhuvan/sansad-archive/actions/runs/36072353217)
at 23:48 UTC on 2026-09-24. Its HF checkpoint reports 2,198 of 2,198
downloaded LS 01/II items inventoried, zero missing attachment ledgers, and
2,198 PDF bitstreams mapping to 1,365 distinct bytes. This particular scope
has no second PDF bitstreams. Its updated published tranche has 1,365 selected
original PDFs and 2,151 pages with both local and free-model output; the
manifest now records each official bitstream URL, ID, name, selected status,
and PDF SHA-256. Publication verification and remote file-identity checks
reported zero failures. This run predates the new `attachment_complete`
marker field, so a later planner pass must write a current marker before the
snapshot is skipped. The earlier live two-PDF LS 17/IX canary verified that
both selected and additional official originals survive the complete local
acquire-to-publication path; a full cloud multi-PDF tranche remains unproved.

The backfill exposed a publication identity weakness: the automatic tranche
label was a hash of `pages.jsonl.zst` alone. Its updated PDF manifest therefore
replaced the earlier selected-only bundle at the same HF path. The automatic
label now hashes both page text and `manifest.jsonl`, including the source PDF
inventory, so future provenance changes create a distinct tranche. Previously
uploaded versions remain in HF Git history, but current HEAD only exposes the
newest version at that old path.
The older pending historical batch `36073146060` was cancelled before it
started, and replacement batch `36074837899` was queued on commit `d05ab41`
with two scopes and one runner. Its planner includes the attachment-complete
marker gate and the manifest-sensitive tranche name; it remains pending behind
the already-active historical batch, not evidence of another scope underway.

The read-only HF inspector reports `checkpoint_status: not_found` while a
scope is still in its first acquisition chunk. It propagates other Hub errors;
checkpoint absence alone is not evidence that its GitHub job stopped.
For historical checkpoints it also distinguishes an old database without an
attachment ledger (`not_recorded`) from a new ledger with missing item
inventories, and counts source PDF bitstreams separately from distinct bytes.
Historical snapshot-complete markers created before attachment inventory are
no longer sufficient for the batch planner to skip a scope. New run summaries
write `attachment_complete=true` only when every downloaded eLibrary item has
an attachment ledger containing its selected PDF SHA; the planner requires
that flag in addition to page coverage, matching census snapshot and live
tranche-file presence. This intentionally queues old selected-PDF-only scopes
for a raw-attachment backfill and verified republication.

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

The [current Hub storage policy](https://huggingface.co/docs/hub/storage-limits)
describes free public storage as *best-effort*, not a guaranteed quota for this
corpus. It asks users to use storage beyond the first few GB responsibly and
evaluates grants for high-impact open-source work case by case, with evidence
of community impact. The full archive's projected hundreds of GB therefore
cannot be promised on a free account without Hub approval. Continue bounded
public uploads and preserve resumable checkpoints; seek a grant before treating
full-corpus HF capacity as secured. No paid model is an acceptable fallback.
At 23:36 UTC on 2026-09-24, the public dataset's current HEAD listed 1,390
files totalling 20.0 GiB by Hub file metadata. That is not an account-quota
reading and excludes any retained historical versions; it confirms that
uploads are working now, not that hundreds of GB have been granted.
The root dataset card had been generated by an older template with a dangling
sentence fragment and no explanation of additional Hindi PDFs. After checking
that the remote card exactly matched the old generated text, it was replaced
with the clarified card at HF commit `d328d398c39706ef05a25e3fc779e972d3433615`.
A fresh remote read matched the new generator exactly. The card now describes
source rights, bounded coverage, separate text layers, bitstream provenance and
the fact that extra original PDFs are not separately text-extracted yet.

## Prerequisites

1. A public GitHub repository containing this project.
2. Repository secrets:
   - `OPENROUTER_API_KEY` for Space Bunny Alpha (required);
   - `HF_TOKEN` with write access to the dataset repository (required);
   - `OPENCODE_API_KEY` for MiMo cross-model verification (optional).
3. Repository variable `HF_DATASET_REPO`, for example `your-user/sansad-corpus`.
   Set `ELIBRARY_SNAPSHOT_ROOT` to an immutable, verified HF census root when
   switching historical batches to a newer dated snapshot.
4. Optional repository variables for directly dispatched pilot sessions:
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

The four-hour `schedule` queues each batch workflow at minutes 17 (current API)
and 47 (historical) of 00, 04, 08, 12, 16 and 20 UTC. Each workflow's
concurrency group allows one active batch and one pending batch, so a long
two-wave batch cannot overlap itself. The per-scope lock separately prevents
two runs for one session. GitHub schedules are best-effort and may start late.
Cron events do not supply `workflow_dispatch` inputs. The current-API batch
therefore explicitly sets `all_pages=true`, `include_ocr=true`, and
`skip_existing=true` for scheduled runs; an explicit `false` still works for
a manual diagnostic dispatch. Without the cron branch, all three silently
became `false` despite their dispatch defaults.
At 20:22 UTC on 2026-09-24, GitHub had not emitted the 20:17 current-API
cron run while batch `36039363151` remained active. An explicit full-coverage
fallback batch `36054404767` was queued on commit `6fdd63f` with
`all_pages=true`, `include_ocr=true`, `skip_existing=true`, and the same
four-scope/two-runner limits. Its pending state is protected by the batch
concurrency group; a later scheduled pending run may replace it, in which
case the cron branch must be checked before treating the replacement as safe.
At 20:34 UTC, the historical batch `36039411879` was still processing its
first scope. A manual continuation `36055804575` was queued behind it on
commit `ed37ac1`. Its planner reads the current `ELIBRARY_SNAPSHOT_ROOT`
variable, so the next pass uses the verified 2026-09-24 eLibrary inventory;
a later scheduled run may replace this pending run under the same concurrency
rule. Neither pending run is evidence that a new scope has started.
At 20:50 UTC, both scheduled events were still absent although GitHub reported
both workflows `active`. After the raw-PDF integrity fix, pending-only fallback
runs `36057524092` (current API) and `36057536325` (historical) were dispatched
at commit `9f6b419`; GitHub cancelled the older pending runs under the declared
concurrency groups. The three active jobs remained in Space Bunny adjudication.
The fallback inputs preserve full-session acquisition, all-pages model coverage,
OCR inclusion, local canonical text, and no paid cross-model verification.
After the native-page audit cloud canary, pending-only runs `36060344843`
(current API) and `36060358281` (historical) replaced those older fallbacks
at commit `557a4eb`. GitHub again left the active jobs untouched; the pending
runs include the verified mixed-route audit code.
At 22:40 UTC, pending-only successors `36068733982` (current API, four scopes,
two parallel) and `36068688868` (historical, two scopes, two parallel) were
queued at commit `6c1a17f`. GitHub cancelled only the older pending runs and
left the active modern and historical model jobs running. The successors
include the transcript-publication guard and missing-tranche marker recovery;
their pending state is not evidence that model work has begun.
At 23:09 UTC, the historical pending successor was refreshed as run
`36071290681` on commit `8c0fc10` so newly acquired eLibrary items retain all
ORIGINAL PDF attachments in immutable HF raw shards. GitHub cancelled the old
pending `36068688868` and left active historical batch `36039411879` running.
The new run is pending, not yet evidence of attachment acquisition.
At 23:15 UTC, direct historical continuation `36069132345` completed LS
01/II on the older selected-PDF code. Its HF checkpoint has 1,365 distinct
selected originals and all 2,151 extracted pages with 2,151 free Space Bunny
transcripts; the read-only archive audit found zero missing or blank model
transcripts and zero nonzero/unknown reported costs. It published tranche
`data/lok_sabha/parliament-01/session-II/tranche-snapshot-local-47af5d6d55d88e8673eb`
with 1,365 PDF-keyed documents, 2,198 source records and 2,151 model pages.
The workflow's local and remote publication checks reported zero failures and
set `snapshot_complete=true`, while `session_complete=false` correctly limits
the claim to the dated census snapshot. That publication predates attachment
backfill and must not be cited as proof that all ORIGINAL bitstream variants
were retained. A direct replay is required because the historical planner
would otherwise skip the snapshot-complete marker.
At 23:18 UTC, direct replay `36072053119` was dispatched for LS 01/II on
commit `5be09aa`, with the immutable 2026-09-24 census root, full scope,
all-pages free-model policy, attachment backfill and publication enabled. It
was in progress at 23:19 UTC; no attachment backfill or replacement publication
is yet proven. Pending historical successor `36072076317` now carries the same
code; GitHub cancelled the older pending `36071290681` without touching active
batch `36039411879`.
At 23:22 UTC, MIME-format inspection was added for extensionless PDF
bitstreams. The direct replay `36072053119` was still at census import, so a
cancel request was sent before it reached attachment acquisition. Replacement
run `36072353217` was queued on commit `43ac90c` for the same scope and full
inputs. Historical pending successor `36072372995` carries that commit;
GitHub cancelled the older pending `36072076317` and kept the active
historical batch running. At 23:23 UTC, `36072053119` was terminal/cancelled
and replacement `36072353217` had started runner preparation; attachment
backfill had not yet begun.
At 23:28 UTC, pending historical batch `36072908147` was queued on commit
`5e56235`, which requires attachment-aware snapshot completion markers. GitHub
cancelled only the older pending `36072372995`; active batch `36039411879`
remained in LS 01/III acquisition. The direct LS 01/II replay remained active
in acquisition. A later batch may revisit II once to replace its pre-marker-
upgrade run summary, even if that replay finishes its PDF backfill first.
At the 23:31 UTC watch, GitHub's scheduled historical batch `36073146060`
replaced the manual pending `36072908147`. It uses commit `66da78a`, which
includes attachment-aware marker semantics; the active batch was untouched.
The scheduled modern pending run `36071728804` similarly replaced the older
manual pending modern batch and retained full scheduled inputs. HF checkpoints
showed LS 01/II attachment backfill at 600/2,198 items (zero extra PDFs in
those first 600), LS 01/III acquisition at 2,000/3,677 records, LS 17/15 at
2,034/4,756 free-model pages, LS 18/6 extracting 2,700/3,499 PDFs, and LS
18/5 extracting 100/5,248 PDFs. All recorded OpenRouter costs were zero.
At 23:34 UTC, a bounded live-source canary of eLibrary item
`49d60eef-b8d0-4c19-83bf-2804f0a8c3d0` (LS 17/IX) exercised the actual
acquire -> LiteParse -> compact publication path in a temporary local directory.
Its two official originals (`AU3055.pdf`, 33,608 bytes, and
`AU3055_hindi.pdf`, 58,974 bytes) had distinct verified SHA-256 values;
the selected PDF produced two extracted pages. The publication manifest
linked both originals, reported one additional PDF, and passed the bundle
verifier with zero failures. The temporary files were removed automatically.
This proves the multi-PDF code path on real source bytes but is not a cloud
HF multi-PDF publication and did not call the free vision model. The ongoing
LS 01/II cloud replay supplies separate all-pages model and HF durability
evidence; inspect its final attachment counts before combining the claims.
`PILOT_*` variables apply only to a directly dispatched session workflow.
GitHub disables scheduled
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
sessions by default. Each queued batch resumes the earliest unfinished scopes.
A scope is marked complete only when its census, acquisition, extraction,
all-page Space Bunny coverage, and publication are proven. A source record
whose official link is demonstrably HTML rather than a PDF is counted
separately and remains visible in the run summary; it cannot hide a transient
download failure. Markers live at
`state/complete/session-complete-<house>-p<parl>-s<session>.json`. The four-hour
schedule re-runs the batch incrementally, so new sessions are picked up
automatically. The planner validates each existing HF completion or skip
marker against its recorded census, acquisition, extraction, model coverage,
and publication evidence. Older unproven marker filenames are ignored and
their scopes are revisited.
For both current-API and historical completion markers, the planner also
requires the recorded HF tranche to still contain its manifest, metadata,
checksum file and first WebDataset shard. A marker left behind after bundle
deletion cannot suppress reprocessing. This is a remote existence check;
publication itself performs byte-level and original-PDF hash verification.

Scopes whose census returns zero records (typically pre-2000 sessions that the
current API lists but does not serve) are classified as empty: the workflow
skips publication, exits successfully, and uploads a skip marker to
`state/skipped/session-skipped-<key>.json`. The planner skips both complete and
skipped scopes, so empty sessions are attempted once, not at every pass. The same
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

Phase 3 is the historical eLibrary collection. It is not exposed as a
session-scoped API, so the workflow imports verified session slices from a
dated census snapshot. Historical acquisition is running, but the raw Lok
Sabha Q&A originals alone are projected at roughly 279 GiB. With one retained
checkpoint copy and one published original, the corpus could exceed 550 GiB
before text and metadata. HF public storage is best-effort, not a guaranteed
free entitlement ([HF storage policy](https://huggingface.co/docs/hub/storage-limits));
a read-only Hub `used_storage` check on 2026-09-25 reported 37,446,288,484
bytes (about 34.9 GiB) already used by this dataset. The observed use is well
below the projected archive size and does not establish a future free quota.
A storage grant from datasets@huggingface.co is still needed
for confidence in a complete long-term archive. A quota failure stops the
run without switching to paid storage. Expect weeks of wall-clock at current
batch parallelism.
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
The manual **Refresh eLibrary Lok Sabha census** workflow can append a bounded
new-accession prefix to a dated HF snapshot. The
[DSpace REST contract](https://github.com/DSpace/RestContract/blob/dspace-7_x/search-endpoint.md#matching-dspace-objects-search-results)
documents the sort parameter; the live eLibrary endpoint accepted
`sort=dc.date.accessioned,desc` and returned stable,
descending pages in repeated probes. The refresh verifies the base snapshot's
SHA-256 and ID uniqueness, requires the new-ID prefix length to equal the
live-minus-base count, checks ten pages of known IDs beyond that boundary,
rejects cross-page duplicates or changes in source count/order, and uploads
the new compressed snapshot plus manifest in one HF commit with remote LFS
hash verification. The old dated snapshot is retained. Even when the total
count has not changed, the same known-ID overlap
is checked so a new accession offset by a deletion at the head fails closed
instead of being called unchanged. The overlap is capped to the collection
size for small inventories. This remains an incremental inventory of newly
accessioned items, **not** a full recrawl: deletions and
metadata changes among older items require separate reconciliation. A failed
boundary check publishes nothing.
For that full reconciliation, a read-only 2026-09-25 probe found that the
official DSpace endpoint accepts `sort=dc.date.accessioned,asc`: two pages of
three items were accession-ascending, and two immediate reads of the first
100 IDs matched exactly. Ascending accessions would normally keep older page
offsets stable when new items arrive at the end, but this is only a small
probe. Timestamp ties, deletions, server reindexing, and metadata edits still
require page-boundary checks, an end-of-crawl ID/count reconciliation, and
resumable dated shards before any full-refresh claim. The current incremental
snapshot remains the active ingestion inventory until such a recrawl exists.
The new **Reconcile eLibrary Lok Sabha census** workflow implements a bounded
two-pass recrawl under `state/census/full-scan-2026-09-25/`. The first ascending
pass writes normalized records in immutable 100-page HF shards, with page
hashes, accession boundaries and remote byte verification. Repeated scheduled
jobs resume from contiguous shard manifests. The second pass refetches the
same fixed prefix and requires every normalized page hash to match, catching
offset shifts and relevant metadata changes. It tolerates only new accessions
appended beyond the starting count; an earlier insertion, deletion or edit
fails closed. Before publication it checks the boundary pages again, verifies
the complete first-pass file set, recomputes every decompressed shard page hash
and row count, compares IDs against the dated base, and
publishes a separate snapshot plus manifest with new/removed-ID counts.
The official source does not provide transactionally frozen search results,
so even two matching passes are evidence for a dated crawl, not a guarantee
that the live site will never change. The final snapshot is not switched into
historical ingestion automatically. A 100-record live ascending page probe
returned 1,158,768 total items with monotonically increasing accession times;
the full two-pass run still needs to complete. The 100-page cloud canary
[`36078818890`](https://github.com/bebhuvan/sansad-archive/actions/runs/36078818890)
succeeded on 2026-09-25. Its HF first-pass shard has 100 pages and 10,000
normalized records; an independent remote read matched its row count, SHA-256
and byte size, and confirmed its LFS identity. Continuation run
[`36079510594`](https://github.com/bebhuvan/sansad-archive/actions/runs/36079510594)
completed its bounded first-pass allocation at page 2,499. Its 25 contiguous
100-page HF shards were independently rechecked for manifest continuity,
remote file size and SHA-256; decompressed records will be checked before
final snapshot publication. Continuation
[`36094510565`](https://github.com/bebhuvan/sansad-archive/actions/runs/36094510565)
has started from the same root. Neither run yet proves second-pass agreement
or a replacement snapshot.
Refresh run `36047393238` appended exactly 3,500 records to the August base
and published `state/census/snapshot-2026-09-24T192308Z` (SHA-256
`9c2efb8cdfb624398b8468faa82e1c5c771a7f28b8e808e056b3a266ad58aeb9`,
329,911,157 bytes). The manifest reports 1,596,844 total records, including
1,158,768 eLibrary Lok Sabha questions, and records 1,000 known IDs checked
beyond the new-item boundary. An independent HF LFS read matched its byte
size and SHA-256. Historical batch planning now pins the repo variable's
snapshot root into every child run, so a later variable change cannot make
one batch plan against one inventory and import another. The current active
workers remain on the August snapshot until their runs finish.
No-publication eLibrary canary `36048152991` imported this exact new snapshot
into LS 02/IX at commit `73cf2d7`: its run summary records the SHA-256 above,
3/3 OCR pages with stored Space Bunny outputs, a successful three-page
standalone Tesseract layout audit, `publish=false`, no tranche, and no
completion marker. This verifies the pinned snapshot import path without
mistaking a two-PDF canary for a complete session.
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
  adjudication chunk. V3 checkpoints append only newly acquired originals to
  immutable, content-addressed raw-PDF shards. SQLite, extraction artifacts,
  and logs live in a separate mutable state archive. Unchanged originals are
  never re-uploaded; the manifest and any new shard use one Hub commit. V2
  cumulative archives migrate as a verified base shard, and V1 single-archive
  checkpoints remain restorable.
- The checkpoint contains SQLite state, raw PDFs, extraction artifacts, and
  event logs. Rendered page PNGs are excluded because they are large and
  regenerable.
- Raw PDF ingestion verifies the `%PDF-` signature and hashes incoming bytes.
  If a content-addressed target already exists, it rehashes that target before
  accepting a deduplicated source (including the concurrent-ingest race).
  A mismatch fails closed instead of attaching another citation to corrupt
  bytes; checkpoint restore and publication perform independent hash checks.
- Extraction selects PDFs lacking a complete run under the current LiteParse,
  routing, and validation configuration. Each batch is durably checkpointed
  before the next; even if one PDF fails, successful work is saved before the
  job fails. It stops at a deadline that leaves two hours for model work and
  publication, resuming from the checkpoint on the next run.
- LiteParse 2.14.7 serializes in-process PDFium parses, so `process-scope`
  uses separate persistent worker processes instead of threads for true
  document-level parallelism. SQLite WAL provides concurrent writes. A dead
  worker is reported as a failure and the wrapper checkpoints completed work
  before stopping.
- Publication succeeds only after the remote HF tranche has exactly the local
  file set, matching sizes, and matching Git blob or LFS SHA-256 content IDs.
  A partial or altered upload cannot produce a completion marker.
- Every publication retains the source PDF. The local verifier checks each
  manifest key has its original PDF and text layers inside the WebDataset
  shard (and the readable files in small, non-compact tranches), in addition
  to the outer SHA256SUMS. A bundle missing an original is rejected even if
  its checksum file has been regenerated. It also hashes each embedded and
  readable original against the full document SHA-256 in the manifest, so a
  substituted PDF is rejected even when the tar and outer checksum agree.
  The original and derived text share
  the source PDF's SHA-256 key, so readers can retrieve and compare both.
- A model-call row is not itself a transcript. Publication now fails if its
  referenced `adjudicated.md` is missing or blank, even under the default
  local-canonical policy. This prevents a page counted as model-covered in
  SQLite from silently losing its separate model layer in the archive.
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

## Read-only overnight monitoring

`python scripts/inspect_hf_progress.py --repo bebhuvan1/sansad-corpus
--scope lok_sabha-p18-s8 --scope elibrary-lok_sabha-p01-sI` reports scoped
acquisition, distinct retained originals, extraction/model coverage, latest-run
page counts by extraction route and validation status, validation-flag counts,
and zero/unknown/nonzero reported-cost counts. The route and status counts each
sum to `extracted_pages`; `review` means that local validation requested
comparison or inspection, not that extraction failed. Cost calls are scoped through the
session's acquired document digests, even if a checkpoint database contains
unrelated sessions; all historical calls for those digests are counted, while
page coverage uses each document's latest complete extraction run. It
downloads only each checkpoint's
mutable state archive into a temporary directory, verifies its size and
SHA-256, reads SQLite without writing to it, and removes the temporary files
on exit. Immutable original-PDF shards are not downloaded for monitoring;
the PDF count is read from the checkpoint manifest. Default 512 MiB compressed
state and 1 GiB SQLite-file caps limit temporary local disk use. This is a
progress check, not a substitute for validating original PDF hashes on restore
or in a publication.
Use `--audit-transcripts` for a deeper, optional check: it streams the same
verified state archive and confirms that each latest-run OpenRouter page has
a distinct, nonblank UTF-8 `adjudicated.md` member. It reports missing and
blank/invalid counts without extracting artifacts or downloading raw-PDF
shards. A live audit found 634/634 present for LS 17/15 and 1,400/1,400 for
historical LS 01/II, with zero missing or blank transcripts in each
checkpoint (22:12 and 22:10 UTC respectively).

## Verified cloud canaries (2026-09-24)

- LS 17/15 run `36030891951` published 10 source PDFs and 34 pages. All 34
  pages had nonempty local and Space Bunny text. A fresh HF download contained
  exactly 50 files; all `SHA256SUMS` entries, original-PDF SHA-256s, and remote
  Git/LFS content IDs matched. This is a bounded tranche, not a complete
  1,499-record session.
- Full-session LS 17/15 run `36060610619` was dispatched at 21:18 UTC on
  2026-09-24 with `limit=0`, `all_pages=true`, OCR inclusion, free Space Bunny,
  `verify_sample=0`, and publication enabled. Its earlier ten-PDF pilot is a
  resumable starting checkpoint, not a completion claim. The complete marker
  and remotely verified PDF/text tranche are still required before calling
  this 1,499-record session finished.
- At 21:32 UTC, the LS 17/15 V3 HF checkpoint contained 1,010 downloaded
  records and 1,010 retained original-PDF entries, with 489 official records
  still awaiting acquisition. Its two immutable raw shards were listed in the
  checkpoint manifest. A remote HEAD check of the new 241,365,034-byte shard
  found matching Hub LFS SHA-256 and size, without a local shard download.
  Only the ten pilot PDFs had completed extraction at that checkpoint; this
  is acquisition progress, not a complete text corpus or session publication.
- By 21:36 UTC, the same run had finished acquisition: all 1,499 official
  records were `downloaded`, all 1,499 distinct originals were in the V3 HF
  checkpoint, and the job moved to extraction. The final 64,081,820-byte
  immutable raw shard's remote LFS SHA-256 and size matched its manifest.
  Extraction/model coverage and a complete publication remained pending.
- At 21:42 UTC, its V3 HF checkpoint showed all 1,499 original PDFs processed
  by LiteParse, with 4,756 latest-run extracted pages. The job entered Space
  Bunny adjudication; only the 34 previously piloted pages had a model layer
  at that checkpoint. Complete publication still requires the remaining
  4,722 model pages, a passing bundle verification, and a remote marker.
- A no-publication same-scope continuation `36063269806` was queued behind
  `36060610619` at 21:43 UTC. The per-session concurrency group prevents
  overlap. Its `limit=0`, all-pages, zero-paid-verification inputs let it
  resume missing model pages if the first run exhausts its time budget, while
  `publish=false` prevents a duplicate tranche if the first run finishes.
  Publication still needs a separate verified pass if this fallback does work.
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
- LS 17/14 continuation `36040021087` migrated an existing V2 checkpoint to
  V3 without re-uploading its two originals, then added one 46,430-byte raw
  shard for a third PDF. A fresh remote restore verified all three original
  hashes against the V3 index. Its published three-PDF/eight-page tranche at
  `data/lok_sabha/parliament-17/session-14/tranche-snapshot-local-6bb1aa0909253c766d2e`
  has eight nonempty local and Space Bunny layers, all with reported cost
  `0.0`; each published `original.pdf` SHA-256 matches its document manifest.
- Historical eLibrary LS 01/I run `36036700034` published two original 1952
  PDFs and three pages under
  `data/lok_sabha/parliament-01/session-I/tranche-snapshot-local-9d0623763e05941fe35c`.
  Fresh HF reads verified both PDF SHA-256s, nonempty local and Space Bunny
  layers on every page, and `0.0` reported cost. This is a two-record canary,
  not a claim that the 2,950-record dated scope is complete. Those two source
  PDFs include one overlapping printed page; both originals remain preserved.
  A second fresh HF read of its manifest and 512,000-byte WebDataset shard
  verified that both embedded `*.original.pdf` bytes hash to their full manifest
  SHA-256 keys; their separate `*.local.md` and `*.adjudicated.md` members are
  nonempty (4,562/3,936 and 8,889/7,936 bytes respectively). The JSON members
  contain one and two pages. This checks the retrievable archive artifact, not
  only the publication code or its success log.
  The strengthened local verifier also passed on a fresh download of all 18
  files in this published tranche: 17 SHA256SUMS entries and both embedded
  and readable original PDF hashes passed with zero failures.
- In the continuing LS 01/I run, an HF checkpoint at 18:18 UTC contained
  1,002 acquired question records mapped to 685 distinct PDF SHA-256s, with
  zero failed acquisitions and 1,948 records still discovered. This is source
  sharing, not 317 missing downloads: the census retains each record-to-PDF
  link while raw storage deduplicates identical bitstreams.
- Full LS 01/I historical continuation `36037513021` was dispatched at
  commit `f4d7c5a`, with acquisition and extraction checkpoints and free-only
  model enforcement. At the 19:41 UTC HF checkpoint, all 2,950 census records
  were `downloaded` with zero acquisition errors and mapped to 1,807 distinct
  retained PDF bitstreams. By the 19:57 UTC checkpoint, all 1,807 PDFs had
  complete LiteParse runs covering 2,826 pages, and the GitHub job had entered
  Space Bunny processing. This is not yet a published or fully adjudicated
  session.
- Historical batch `36038615784` proved its planner selected LS 01/II and
  01/III from the verified dated snapshot. It was cancelled before acquisition
  to use the separate-process LiteParse speed fix; replacement `36039411879`
  at `6bf49de` uses the same scopes sequentially.
  Its LS 01/II HF checkpoint at 19:44 UTC likewise had all 2,198 source
  records downloaded with zero acquisition errors, mapping to 1,365 distinct
  original PDFs. Its 20:02 UTC checkpoint had 1,200 complete LiteParse runs
  covering 1,890 pages. By 20:08 UTC all 1,365 distinct PDFs had complete
  LiteParse runs covering 2,151 pages, and the GitHub job moved to the
  Space Bunny stage. No full-session tranche existed at that point.
- The older full current-API batch `36032423348` was cancelled after its
  acquisition checkpoints were verified: it had entered a single large,
  uncheckpointed extraction step under commit `8f994b9`. Replacement batch
  `36039029404` at `639eb39` was itself cancelled during runner setup to
  pick up true process parallelism. Active replacement `36039363151` at
  `6bf49de` restores the same original PDFs and saves extraction state every
  100 PDFs. Cancellation can lose only uncheckpointed extraction work, not
  originals already on HF. At the half-hour check just after 20:01 UTC, LS
  18/8 had 1,253 stored Space Bunny calls across 17,989 extracted pages and
  LS 18/7 had 600 across 25,029 pages. Every one of those 1,853 calls had an
  explicit zero reported cost; no null or nonzero cost was stored. Both
  scopes were still in the model stage, not complete-session publications.
- At 20:31 UTC, the bounded read-only HF inspector found 4,500 retained
  originals and 17,306 latest-run pages in LS 18/8, including 1,799 pages
  with a stored model layer. LS 18/7 had 6,974 retained originals, 25,029
  latest-run pages and 1,200 model pages. Historical LS 01/I had 1,807
  distinct originals, 2,826 pages and 403 model pages; LS 01/II had 1,365
  distinct originals, 2,151 pages and 200 model pages. All reported model
  costs in those checkpoint scopes were explicitly zero. The latest-run page
  counts exclude superseded pilot runs; they should not be compared directly
  with the earlier all-run LS 18/8 page total of 17,989.
- At 21:32 UTC, the same four live scopes had respectively 2,799, 2,400,
  1,003 and 800 latest-run model pages. That is 1,800 more than at the 21:02
  UTC read. Their checkpoint model-call costs remained explicitly zero, with
  no unknown or nonzero reported costs. All four GitHub jobs were still in
  adjudication, so no complete-session marker was inferred from page gains.
- The 22:02 UTC read found 3,399, 3,000, 1,403 and 1,200 model pages in those
  four scopes, plus 434 in full-session LS 17/15. That is 2,200 new
  model-covered pages since the 21:32 check. All checkpointed OpenRouter calls
  still had explicit zero cost; no unknown or nonzero calls were recorded.
  The five jobs remained active, with both batch continuations and the safe
  17/15 same-scope continuation pending.
- A read-only quality breakdown from the same checkpoints counted LS 18/8's
  17,306 pages as 14,534 native and 2,772 OCR, with 1,353 flagged `review`.
  Historical LS 01/II's 2,151 pages were 333 native and 1,818 OCR, with
  2,017 flagged `review`. Full-session LS 17/15's 4,756 pages were 4,302
  native and 454 OCR, with 327 flagged `review`. Both route and status totals
  reconcile to the latest-run page count in all three scopes; these are
  checkpointed local-validation flags, not adjudicated accuracy rates.
- The direct historical LS 01/I run `36037513021` finished successfully at
  22:23 UTC after eighteen 100-page model chunks, each with zero page failures
  or rate-limit/fatal flags. Its work deadline stopped a partial pass: the
  final HF state has 1,803 of 2,826 pages model-covered, all 1,807 distinct
  originals retained, and the run summary explicitly says
  `session_complete=false` with no publication. The pending historical batch
  is the planned continuation; a green workflow run alone is not a complete
  archive claim.
- The historical batch `36039411879` finished its LS 01/II child at 22:43
  UTC with 1,800 of 2,151 pages model-covered, all 1,365 original PDFs
  retained, and no complete publication. It then started its second planned
  scope, LS 01/III, so the pending successor cannot resume II yet. Direct
  same-scope continuation `36069132345` was dispatched at 22:44 UTC on
  `1c60c8e` with the pinned 2026-09-24 snapshot, all pages, both extraction
  layers, free model, and publication enabled. GitHub confirmed it running
  while the III child remained active; II's earlier child had completed, so
  no same-scope writer overlaps.
- The historical LS 01/II checkpoint's 2,017 review pages have 1,843
  inconsistent-table-width flags and 1,803 native/OCR numeric-disagreement
  flags; these can co-occur on a page. Only one page has a low-OCR-confidence
  flag. The modern LS 17/15 checkpoint has 33 empty-text flags among 4,756
  locally extracted pages. Flags identify comparison and image-inspection
  priorities; they do not establish which transcript is correct. The
  read-only monitor now exposes the individual counts without downloading
  original-PDF shards.
- LS 18/1 returned zero records despite appearing in the official session
  inventory, so its green skip run is not an extraction canary. The planner
  retains this distinction in the marker evidence.

## Accuracy policy

- Every page gets a local extraction and, by default in cloud runs, a Space
  Bunny Alpha transcription (`all_pages=true`, reasoning effort `low`,
  temperature 0). OCR still runs on every OCR-routed page, so scanned pages
  carry both an OCR candidate and a model candidate.
- LiteParse 2.14.7 supplies native extraction and built-in Tesseract OCR for
  pages whose native text is missing or suspect. The
  [PyPI package metadata](https://pypi.org/pypi/liteparse/json) still listed
  2.14.7 as the current release on 2026-09-24. The model adapter rejects
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
  adjudication canonical. A verified near-white page is an exception under
  either policy: publication now emits empty canonical text, retains the raw
  local and model layers, records the measured image metrics, and flags any
  nonempty transcript for review. Invalid visual metrics fail publication.
  Promoting OpenRouter text to canonical also requires valid per-page visual
  metrics; an older unmeasured model artifact cannot bypass the blank guard.
  This guard applies to newly built tranches; existing immutable tranches
  remain unchanged and need an explicit corrected publication to gain it.
  Space Bunny sees the local candidate as a prompt
  hint, so these are separate outputs but **not independent witnesses**.
  Independence requires source-image review or the optional separate-model
  audit; disagreement flags alone cannot decide which is correct.
- A read-only quality check of the LS 18/8 HF checkpoint dated
  2026-09-24 19:37 UTC found all 853 stored Space Bunny page artifacts present
  and nonempty. Its visible-number multiset differed from LiteParse on 328
  pages (38.5%). This is a disagreement rate for the first checkpointed
  model pages, **not** an error rate or evidence that either layer is right.
  Keep both layers and source PDFs; investigate disagreements against page
  images before considering a canonical-policy change.
  A later 1,253-page checkpoint had 466 raw visible-number disagreements;
  excluding only standalone `Page N of M` footer lines reduced that to 293.
  One source-image spot check confirmed the model retained a printed page
  footer that LiteParse omitted while both matched the table's financial
  figures. Model/local content-number flags and audit sampling now exclude
  that pagination-only noise; the transcripts still retain their original
  wording, and the audit report separately records raw-number disagreement.
- A bounded image-only Space Bunny test on the visible 1952 LS 01/I page 25
  cost `0.0` but changed the printed “Government are, however, doing
  everything possible” to “Government are not doing everything possible.”
  The candidate-assisted stored transcription matched the scan on this
  consequential phrase. This single counterexample rules out a blind
  corpus-wide switch to image-only prompting. Two source PDFs had an exact
  pixel-identical first page but slightly different local candidates and
  model responses; the system therefore does not silently reuse one model
  response for merely identical page images.
- A source-image check of two distinct 1952 two-column scans (printed pages
  25 and 27) exposed a local-layout failure: LiteParse Markdown introduced
  seven and six table-separator rows (34 and 27 separator cells) into ordinary
  prose and mixed words within
  some questions. Tesseract `--psm 3` on the same rendered pages preserved the
  left-column-then-right-column reading order substantially better. Changing
  LiteParse's output format from Markdown to text did not fix its two-column
  ordering, so this is a layout-reconstruction issue, not merely Markdown
  syntax. The stored model layer was generally closer to the visible scan in
  these samples, but it is candidate-assisted; a separate Tesseract transcript
  is a promising independent QA signal, not yet a validated corpus-wide
  replacement. Preserve the originals and both existing text layers while
  evaluating that signal on more layouts, including genuine tables.
- Each future cloud pass samples up to 24 pages for a separate, free
  Tesseract `--psm 3` image audit. At least half are selected first by a
  fixed-seed uniform random draw from all extracted pages and labelled
  `random_baseline`. The remaining places cover a missing native/OCR route,
  empty local text, LiteParse layout suspects (three or more Markdown table
  separators), and LiteParse/Space Bunny visible-number disagreements. Any
  unused places are labelled `random_fill`, not mixed into the probability
  sample. The report records every selection stratum. Only the random baseline
  is suitable for a prevalence estimate, and its small size still warrants
  wide uncertainty; the enriched cases are for failure discovery.
  It uploads the independent transcript plus
  similarity/numeric-agreement diagnostics and both sides of each numeric
  disagreement under the run's `verification/`
  path. This is non-mutating evidence, not an automatic canonical-text switch;
  OCR and Space Bunny can both make errors, so disputed pages still require
  source-image review. An empty Tesseract transcript is accepted only when the
  rendered image has at most 25 pixels darker than 250/255; the report records
  the pixel count and flags nonempty local or model text on that visually blank
  page. LiteParse's empty Markdown code-fence wrapper does not count as local
  content when its text field is empty. This sampled canary fails on empty OCR
  from a visibly inked page; the full sidecar retains such a row with a
  page-keyed review flag so one OCR miss cannot block the corpus. In LS 17/15,
  official PDF `AU691.pdf` (SHA-256 `8c2e2bc9a131d7cce87c2f60247f6f1329e6615ddc52bd9996389b6825210386`)
  had an all-white third page: LiteParse and Tesseract were empty, while Space
  Bunny wrote `Y 1300 . Y 1301 .`. The model text remains stored as evidence
  and is explicitly flagged; canonical local text stays empty. A subsequent
  verified HF audit covered all 4,756 LS 17/15 pages and found 33 visually
  near-white pages, all with nonempty Space Bunny text; none had nonempty
  local text. The separate full-OCR sidecar had processed 1,900 pages at that
  checkpoint, with no OCR text on its measured blank pages.
  The first local end-to-end audit on a 1952 scan selected
  one page from five OCR candidates, completed in 8.2 seconds with no error,
  and reported text similarity 0.3625 to LiteParse Markdown versus 0.9429 to
  the stored model transcription. Both numeric comparisons disagreed with
  Tesseract, so similarity alone cannot adjudicate that page.
  The full LS 01/II cloud audit in run `36072353217` covered twelve pages
  (two route baselines, four layout suspects, four numeric-disagreement
  suspects, two random). Model/Tesseract text similarity exceeded
  LiteParse/Tesseract similarity on all twelve; the medians were 0.549 and
  0.192 respectively. Exact visible-number agreement with Tesseract was only
  one of twelve for the model and zero for local text. This enriched sample
  diagnoses a serious historical OCR/layout problem but cannot estimate its
  prevalence. We redownloaded the official two-page PDF with SHA-256
  `0ba8f0ab53d47041de951ff158d65f749218d6fdb0ca1428bce62174d372bd88`
  and inspected page two against the published layers: it is a degraded
  two-column scan, LiteParse inserted spurious tables and scrambled words,
  while Space Bunny largely preserved the visible questions and column order.
  Neither model/Tesseract agreement nor a single visual check proves perfect
  transcription; keep the source PDF, both layers, and review flags together.
- A two-document cloud canary (`36045023595`) exposed two workflow-boundary
  failures before this audit could be trusted: the runner lacked the system
  `tesseract` executable, and `inputs.publish || 'true'` treated an explicit
  `publish=false` as the default `true`. That canary therefore published a
  bounded, remotely verified two-PDF tranche at
  `data/lok_sabha/parliament-02/session-IX/tranche-snapshot-local-1a185ab8b95638656abd`
  despite the no-publication request; it is not a complete-session claim.
  The workflow now installs the free Tesseract binary before auditing,
  makes audit failure fail the pass instead of appearing green, and converts
  Boolean inputs explicitly so `false` remains `false`. Follow-up cloud run
  `36045777622` verified the fixes: the installed Tesseract 5.3.4 audited all
  three OCR pages with zero failures, uploaded the transcript report under
  `state/runs/20260924T190806Z-lok_sabha-p02-sIX/verification/`, and skipped
  publication. Its run summary has `publish=false`, an empty tranche path and
  no completion marker; the three stored model pages each report cost `0.0`.
- No-publication cloud canary `36050779676` at commit `fe069f1` verified the
  stratified audit on the same bounded scope. Tesseract 5.3.4 produced three
  nonempty transcripts with zero failures; one page was selected for layout
  and two for model/local numeric disagreement. Fresh HF reads found all three
  selection reasons and transcript hashes under
  `state/runs/20260924T195347Z-lok_sabha-p02-sIX/verification/`. Its run
  summary again has no tranche or completion marker. This validates the
  audit/report path, not the accuracy of either transcription.
- Footer-aware cloud canary `36052696228` at commit `5cc684d` again audited
  all three OCR pages with Tesseract 5.3.4 and no failures. The fresh HF report
  at `state/runs/20260924T201110Z-lok_sabha-p02-sIX/verification/` contains
  both content-number and raw-number disagreement fields, three transcript
  hashes, and selection reasons. The run had `publish=false`, an empty
  tranche path, and no completion marker.
- Mixed-route audit code was exercised in no-publication cloud canary
  `36059585013` at commit `f07afc2`. Its LS 17/15 checkpoint contained 34
  native-routed pages; the independent Tesseract 5.3.4 pass selected twelve,
  stored twelve nonempty transcripts, and reported zero audit failures. A fresh
  HF read of `state/runs/20260924T211343Z-lok_sabha-p17-s15/verification/`
  confirmed all twelve `liteparse_route=native` records, selection strata,
  and the report. The run summary says `publish=false` and has no tranche.
  This proves the native route can be audited in cloud; mixed-route selection
  is covered by a deterministic unit test, not yet a mixed-route cloud sample.
- Model output is re-validated at publication time: empty output, replacement
  characters, inconsistent table widths, and numeric disagreement against the
  local candidate are recorded per page as `canonical_validation` and in the
  page tables. Numeric comparison counts Markdown link labels but not their
  destinations, so a URL rendered as `[visible URL](same URL)` is not double
  counted. Flags create review evidence, never silent corrections.
  Standalone `Page N of M` lines are ignored only for the model/local content-
  number comparison, because one extractor may omit a footer; the full text
  layers and raw-number audit remain available for pagination review.
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
  artifact directory. New calls also store conservative rendered-image ink
  metrics and separate quality flags when the selected local or model text is
  nonempty on a visually blank page; the raw responses remain unchanged. An
  empty LiteParse Markdown fence is no longer sent to Space Bunny as candidate
  page content. A genuinely empty model response is accepted only with stored
  near-white image evidence; its empty transcript remains a distinct model
  layer in the checkpoint and WebDataset. Publication and the checkpoint
  inspector reject empty responses without that evidence.
- Full image-only LiteParse OCR shards produced by the updated extractor record
  rendered-image ink metrics for every page, including nonempty OCR output.
  `ocr-nonempty-on-visually-blank-page` flags possible OCR invention without
  rewriting the transcript. `ocr-empty-on-visibly-nonblank-page` records an OCR
  omission while preserving the empty text and the separate local/model
  layers. Neither flag adjudicates what the page says. The screenshot used for
  image-only OCR supplies the metrics; reused selected-local OCR is rendered
  independently for visual evidence, but its transcript is the same artifact.
  Existing shards remain immutable and resumable, so an in-flight sidecar can
  contain older shards without all-page metrics. Absence of that evidence is not a
  clean-page finding; retrospective all-page audits remain necessary.
- After each bounded full-OCR continuation, `scripts/audit_published_ocr.py`
  checks the published `pages.parquet` SHA-256, OCR shard SHA-256s, exact page
  identities and separate local/model text. It publishes an immutable report
  under `audits/full-ocr-model-visual/<scope>/`. The report distinguishes OCR
  pages not yet processed, older OCR rows without image metrics, visually
  assessed pages, blank-page conflicts for each text layer, and empty model
  transcripts and empty OCR on visibly inked pages as separate review queues.
  It does not call OCR or the image heuristic transcription ground truth. The
  report stratifies numerical comparisons by OCR origin. A
  `selected-local-rasterized-ocr` row reuses the local OCR transcript exactly;
  its agreement with local text is not a second vote. A
  `sidecar-rasterized-ocr` row is a separate execution of the same OCR method,
  not an independent ground truth either. Space Bunny received the local
  candidate on nonempty pages, so model/local agreement also is not an
  independent vote. These counts are review queues, never a corpus-wide
  accuracy rate.
- Older sidecars receive a separate bounded visual-evidence backfill through
  `visual-evidence-layer.yml`. It restores the verified original PDFs from the
  HF checkpoint, renders every page at the configured 250 dpi, and stores
  exact page-keyed pixel measurements in immutable 100-page HF shards under
  `layers/visual-evidence/<scope>/`. It resumes only after validating every
  earlier shard's checksum, contiguous inventory, page identity and visual
  counts. The published-layer audit joins these measurements with older OCR
  shards and the separate local/model text; contradictory measurements from
  newer OCR shards fail closed. Neither process rewrites text or original PDFs.
- New image-only OCR and model/visual-evidence runs use the same LiteParse
  page screenshot subprocess with a 180-second per-page deadline. A timeout
  or renderer failure names the affected PDF page and fails the current
  bounded chunk; earlier immutable HF shards remain resumable. This prevents
  one hostile PDF render from silently consuming an entire 330-minute Actions
  job. The already-running LS 17/15 OCR job started before this OCR-side
  change and retains the older in-process screenshot path until it finishes
  or is resumed from a later commit.
- LiteParse Python is pinned to `2.14.7`, the latest version shown on its
  [PyPI project page](https://pypi.org/project/liteparse/) on 2026-09-25;
  extraction artifacts record that exact engine version.
- The published-layer report also compares visible numeric tokens in the
  separate full-image OCR and Space Bunny Markdown when both are nonempty.
  Standalone page-count footers and Markdown link destinations do not create
  false disagreements. The report records page-keyed OCR-only/model-only
  counts and bounded token samples as a review queue, distinct from the
  image-proven blank-page conflicts; neither transcript is ground truth.
  When local, full-image OCR and model text all contain a visible number, the
  report additionally labels the exact three-way token-multiset pattern:
  all equal, one of the three possible agreeing pairs, or all different.
  Pages without any number are excluded from this triad denominator. These
  labels prioritize source-image review; the model saw the local candidate,
  so a local/model match is not an independent vote or accuracy estimate.

### Verified 2026-09-25 milestones

- LS 17/15 completed in cloud run `36086340826`. Its Hugging Face completion
  marker points to `tranche-snapshot-local-7b6e50b4a7bc30622853`: 1,499
  documents, 4,756 unique pages, 4,756 stored Space Bunny layers, and original
  PDFs in the compact WebDataset. The required tranche files exist; downloaded
  `manifest.jsonl`, `metadata.json`, and `pages.parquet` match `SHA256SUMS`.
  The canonical policy remains `local`. The independent 24-page audit found
  five visually blank pages, all with spurious nonempty model text. Their
  canonical local text is empty. This run predates the per-page visual flags;
  the sampled audit report identifies the five cases, not every such page in
  the scope. Completion is coverage evidence, not an accuracy certificate.
- A read-only join against the first 800 LS 17/15 full-OCR sidecar rows verified
  six OCR-empty, visually blank pages; Space Bunny text was nonempty on all
  six. The other 794 older rows lacked image metrics, and 3,956 pages had no
  OCR sidecar yet. This is a confirmed conflict list, not a prevalence estimate.
- Cloud audit run `36089041568` published the immutable 900-page LS 17/15
  comparison report at `audits/full-ocr-model-visual/lok_sabha-p17-s15/`
  `ocr-pages-00000900-3fae467394b25156.json`. Its downloaded SHA-256 matches
  the filename prefix; it records six visually blank pages with nonempty model
  text, 894 older OCR rows without image metrics, and 3,856 pages awaiting the
  full-OCR sidecar. The report is evidence for those six pages only.
- LS 01/II's separate full image-only LiteParse OCR sidecar completed in run
  `36085913818`: 2,151 unique page keys in 22 contiguous shards. All remote
  shard sizes and SHA-256 hashes were independently rechecked, as were the
  source completion marker and original-PDF inventory hash. The first 600
  pages use the original shard schema without an origin field; later shards
  disclose 1,274 provenance-checked reuses of selected image OCR and 277 fresh
  sidecar OCR results. No page in these shards needed blank-page evidence.
  Cloud audit run `36089167501` joined all 2,151 pages and published a
  SHA-256-verified report at `audits/full-ocr-model-visual/lok_sabha-p01-sII/`
  `ocr-pages-00002151-1ee82743e0aa1f8b.json`. None of these older OCR rows
  had all-page image metrics, so this cannot establish a zero blank-page-
  conflict rate; the separate completed visual backfill below supplies that
  missing evidence without changing the OCR shards.
- The first visual-backfill canary (`36089735977`) failed before rendering on
  a standalone-script import path; no HF shard was written. Commit `128e8a5`
  fixed the entry point and added an outside-checkout CLI test. The corrected
  100-page LS 01/II canary (`36089996509`) published and independently
  verified one visual shard and an updated comparison report: 100 measured
  pages, zero visually blank among those 100, and 2,051 still unmeasured.
- LS 01/II continuation `36090217938` completed all 2,151 visual pages in
  22 contiguous HF shards. An independent local replay verified every OCR
  and visual shard checksum, page identity, inventory and completion marker.
  The updated cloud report from run `36090800082` is at
  `audits/full-ocr-model-visual/lok_sabha-p01-sII/`
  `ocr-pages-00002151-01e9f03e2cf61252.json`; its downloaded SHA-256 matches
  the filename prefix. All 2,151 pages have visual measurements; none met the
  strict near-white blank-page threshold. Image-only OCR and Space Bunny
  disagree on visible numeric tokens on 2,094 of 2,151 comparable pages.
  This is a disagreement count, not a model or OCR error rate. The LS 17/15
  visual continuation `36090227461` completed all 4,756 pages in 48
  contiguous, verified shards. Cloud audit `36093301405` published the
  SHA-256-verified report at `audits/full-ocr-model-visual/lok_sabha-p17-s15/`
  `ocr-pages-00001900-58ec3218bdfd7aca.json`: all pages visually assessed,
  33 near-white pages with nonempty model text, none with nonempty local text,
  and 893 numeric disagreements among 1,900 pages with both OCR and model
  text. Of the 33 blank-page model responses, 22 were exactly `[ILLEGIBLE]`;
  both model prompt variants now explicitly require an empty response for a
  blank image. The image-based guard still checks the result independently.
  The full OCR sidecar remained incomplete at this checkpoint.
  The first LS 17/15 full-OCR continuation `36087115763` safely checkpointed
  2,000 of 4,756 pages; a later 100-page shard took much longer than earlier
  shards but completed. Direct continuation `36093800075` was dispatched from
  the bounded-renderer commit for the remaining 2,756 pages. It failed before
  its first new shard: isolated rendering no longer initialized LiteParse's
  private tessdata cache in the parent process, and the parent OCR library
  could not find `eng.traineddata`. The workflow now discovers the installed
  `tesseract-ocr-eng` data file and exports its directory as `TESSDATA_PREFIX`.
  A local image-only OCR test passed with that explicit setting. No completion
  claim is made until the resumed cloud run and its remote shards are checked.
  The first retry `36094258753` failed during dependency setup because the
  runner image lacks `rg`; no OCR step or HF write occurred. The tessdata
  lookup now uses `awk`, which was verified against the installed package
  locally. Retry `36094489013` passed installation, credential checks and
  verified checkpoint restore, then entered the OCR step. Its first new
  100-page shard was independently audited: 2,100 contiguous OCR rows now
  match the immutable published page keys and remote shard hashes. The audit
  still finds 33 model-text conflicts on the 33 near-white pages across the
  complete visual layer, with zero local/OCR text conflicts; numeric
  disagreements stand at 1,005 of 2,100 comparable pages. This verifies
  recovery through page 2,100, not full OCR completion.
  A newer read-only audit verified 2,700 contiguous OCR pages against the
  still-complete 4,756-page visual inventory: 33 near-white/model-text
  conflicts, zero empty model transcripts on visibly inked pages, and 1,314
  OCR/model numeric disagreements among 2,700 comparable pages. These are
  review signals, not adjudicated error rates.
  Cloud audit `36095621559` advanced the verified OCR prefix to 2,900 pages
  and published `audits/full-ocr-model-visual/lok_sabha-p17-s15/`
  `ocr-pages-00002900-dab14eb71b0d9a57.json`; an independent download matched
  its SHA-256 filename prefix. It reports zero empty model transcripts on
  visibly inked pages, 33 blank-page model-text conflicts, and 1,416
  OCR/model numeric disagreements among 2,900 comparable pages.
  A read-only three-way replay at 3,100 OCR pages found 3,025 pages with a
  number in at least one of local, OCR or model text: 1,470 all equal, 925
  local/model equal only, 306 local/OCR equal only, 32 model/OCR equal only,
  and 292 all different. The model was prompted with local text, so the 925
  local/model matches are not independent corroboration; all four disagreement
  groups remain source-image review queues.
  Cloud audit `36096084059` published the first three-way report at
  `audits/full-ocr-model-visual/lok_sabha-p17-s15/`
  `ocr-pages-00003300-d0bb25478052a190.json`. Its independently downloaded
  SHA-256 matches the filename prefix. At 3,300 verified OCR pages, 3,220
  pages had numeric content in all three text layers: 1,570 all equal, 993
  local/model equal only, 323 local/OCR equal only, 34 model/OCR equal only,
  and 300 all different. Blank-page and empty-model-on-visible findings
  remained 33 and zero respectively.
  The OCR continuation `36094489013` later stopped at LS 17/15 original
  `AU951.pdf`, page 2 (SHA-256
  `bf29f96793d0d058b8c50ad4facd5579eaeb13920f0d4b3341083fe387990a44`).
  The 250-dpi render contains a small centred `*****` mark (531 dark pixels)
  and the PDF text layer contains `*****`, but LiteParse returned empty text and
  an empty fenced Markdown block. Alternate Tesseract page-segmentation modes
  misread the mark as letters, so substituting them would manufacture text.
  The sidecar now permits an empty OCR transcript only with measured image
  evidence and the exact `ocr-empty-on-visibly-nonblank-page` review flag;
  shard replay validates both. This is a general omission policy, not an
  exception keyed to that PDF. Its original, local extraction and model text
  remain untouched. The audit excludes the empty fenced block from numeric
  comparisons and reports this class separately.
  Commit `96f1057` resumed the cloud OCR workflow as run `36096755991`.
  An independent read-only replay after its first shard verified 3,500
  contiguous OCR pages against the published page inventory, every shard
  checksum and page identity, and the complete 4,756-page visual sidecar.
  The audit reported exactly one empty-OCR-on-visible review item, matching
  `AU951.pdf` page 2 and its 531 dark pixels; 1,256 OCR pages remained at
  that checkpoint. The job was still processing, so this does not establish
  sidecar completion.
  Run `36096755991` subsequently completed. An independent read-only audit
  verified all 4,756 OCR pages in 48 contiguous shards against each remote
  checksum, the published page inventory and the full visual sidecar;
  `complete.json` matched the inventory and original publication marker.
  The run-published report is
  `audits/full-ocr-model-visual/lok_sabha-p17-s15/`
  `ocr-pages-00004756-7af592cf632490e3.json`, and its independently
  downloaded SHA-256 is
  `7af592cf632490e3861939a4a3b9cdaab85ab9ad2e82b5f030e0d0a3ba94fdd1`.
  It reports two empty-OCR-on-visible pages, 33 model-text conflicts on
  near-white pages, and no local or OCR text on those blank pages. The second
  OCR omission is official `AU949.pdf` page 3 (SHA-256
  `f2f785022fa968b7c7be8b274dfae57413458292ab2753a4666bdf26d101e869`):
  its source PDF hash matches the manifest, and both its text layer and visual
  render show only `*****` (531 dark pixels). These two omissions remain
  review items; their native local and model transcripts were not replaced.

- Historical LS 01/I batch `36080678021` completed on 2026-09-25. The
  independently inspected HF checkpoint contains all 2,950 attachment
  records, 1,807 distinct retained PDF bitstreams, 2,826 extracted pages and
  2,826 separate Space Bunny transcripts; artifact replay found zero missing
  or invalid transcripts and all 2,826 reported calls cost `0.0`. Its
  `state/snapshot-complete/snapshot-complete-lok_sabha-p01-sI.json` marker
  matches the scope and verified raw inventory. A fresh read checked the
  published `pages.parquet` and `metadata.json` SHA-256s and found 2,826
  unique page keys across 1,807 PDFs, every page with a model layer. All nine
  files named in the tranche `SHA256SUMS` exist on HF; the remote LFS hash of
  the 741,416,960-byte WebDataset tar matches its recorded checksum. This
  proves the completed local/model/original publication. Direct full-OCR run
  `36103343026` subsequently completed a 29-shard sidecar. An independent
  replay verified all 2,826 OCR page identities, shard hashes, completion
  marker and image measurements; it found zero visually blank pages and zero
  empty OCR or model transcripts on visibly inked pages. The preliminary
  unstratified numeric report showed 1,922 local/OCR matches where the model
  differed. A provenance-aware replay showed that 1,919 of these used the
  exact selected-local OCR artifact, leaving only three from separate
  sidecar executions. Of 2,826 OCR rows, 2,019 are reused selected-local OCR
  and 807 were newly run by the sidecar. The latter are still the same OCR
  method, so these agreement counts cannot adjudicate model accuracy. The
  model and OCR disagree on visible numbers on 2,639 pages; this is a
  source-image review queue, not a model error rate or reason to switch
  canonical text automatically.

- The scheduled full-OCR batch `36109890871` and direct provenance audit
  `36109137755` exposed a marker-version failure on LS 01/II. Both sidecars
  had complete, checksum-verified coverage of the same 2,151 published page
  keys and the same raw-PDF inventory SHA-256, but a later publication had
  changed the snapshot marker's byte hash. The OCR completion file had been
  refreshed to the new marker; the immutable visual completion file still
  named the earlier one. An exact marker-hash check therefore falsely failed
  the audit. The audit, OCR/visual completion checks and OCR batch planner now
  compare the stable marker path, checkpoint path and raw-PDF inventory hash,
  while independently validating the current publication checksum, every
  sidecar shard and exact page keys. A changed raw inventory still fails.
  Existing completion files are left immutable when only the publication
  marker bytes change; reports expose that marker-version difference. A local
  replay on the actual LS 01/II HF artifacts passed all 2,151 pages and
  reported `publication_marker_changed_since_sidecar=true` only for the visual
  layer. Of its 2,151 OCR rows, 1,274 reuse the selected local OCR artifact
  and 877 are new sidecar executions, so agreement is interpreted by origin.

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
