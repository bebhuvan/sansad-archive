# One-session-per-House publication pilot

Status date: 2026-08-12 (Asia/Kolkata)

This pilot tests the complete, resumable path from the official Parliament
inventories to a public Hugging Face research release for one Lok Sabha scope
and one Rajya Sabha scope. It deliberately records what was available at a
specific cutoff; neither scope is described as a complete session while the
official inventories can still change.

## Selected scopes and cutoff

| House | Scope | Official records observed on 2026-08-12 | Date range in census |
|---|---|---:|---|
| Lok Sabha | 18th Lok Sabha, session 8 | 4,250 | 2026-07-20 onward |
| Rajya Sabha | session 271 (Parliament number is not supplied by the RS API) | 2,975 | 2026-07-20 onward |

These are matching contemporary scopes and exercise both current Sansad source
connectors. The counts changed during development: the stored pre-refresh
counts were 2,500 LS and 1,574 RS. The official inventories were therefore
refreshed immediately before acquisition. Published metadata must state the
cutoff and observed count, not merely the session number.

## Storage gate

The machine had 7.8 GiB free before acquisition. Range requests (`bytes=0-0`)
were used to read total PDF sizes without downloading sample files. A
24-document distributed sample from each pre-refresh inventory produced:

| Scope | Median PDF | Mean PDF | Initial projected raw total |
|---|---:|---:|---:|
| LS 18/session 8 (then 2,500 records) | 191,452 B | 240,802 B | 0.561 GiB |
| RS session 271 (then 1,574 records) | 142,186 B | 186,434 B | 0.273 GiB |

The post-refresh record counts imply roughly 1.0 GiB LS and 0.52 GiB RS if the
sample means hold. Initial acquisition uses a 6 GiB free-space floor. This leaves room
for extraction artifacts; publication itself duplicates original PDFs inside a
WebDataset TAR, so free space must be checked again before bundle construction.

The 6 GiB gate stopped RS safely after 1,920 new downloads (1,921 acquired
records including the earlier pilot), with zero download failures and 1,054
records remaining. A disk audit found 1.6 GiB of raw PDFs, a 2.5 GiB SQLite
census, 315 MiB of prior census exports, and only about 50 MiB of extraction
artifacts. The remaining RS PDFs were projected at about 260 MiB. The bounded
resume therefore uses a 5.5 GiB floor; projected free space after both
publication TARs remains roughly 3.8--4.0 GiB. Nothing was deleted to make room.

## Processing and review policy

1. Download the official PDF into immutable SHA-256-addressed storage.
2. Extract native text with pinned LiteParse 2.10.1.
3. OCR only empty/broken/full-page-scan routes; do not OCR merely sparse tables.
4. Validate every page. Flags remain visible and never silently rewrite text.
5. Send every page to NVIDIA Nemotron, sequentially. Local validation flags
   remain useful for comparison and human-review priority, not API routing.
6. Nemotron receives the image and transcription rules without the local text,
   making it an independent OCR candidate rather than a correction pass.
7. Preserve local output and model output side by side. Nemotron is the
   canonical machine transcription when successful, not human ground truth.
8. Build and checksum the multi-format tranche before uploading it.

The NVIDIA reviewer uses:

- endpoint: `https://integrate.api.nvidia.com/v1/chat/completions`;
- model: `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`;
- page render: 150 DPI;
- temperature: 0;
- reasoning budget: 1,024 tokens;
- maximum measured global concurrency: 5 requests (split 3 LS + 2 RS);
- each worker waits at least 3 seconds between its own completed requests;
- retryable statuses: 408, 429, 500, 502, 503 and 504;
- exponential backoff with jitter, capped at 45 seconds, honoring
  `Retry-After` when present;
- one retry for socket/read timeouts and six retries for retryable HTTP errors.

Every attempt stores timestamps, HTTP status, safe rate-limit response headers,
request SHA-256, model, render settings, token usage, response JSON and the
resulting Markdown. The API key is read from ignored `.env` and is never stored
in artifacts or publications.

### Concurrency benchmark

Real pages from both scopes were tested at simultaneous wave sizes 1, 2, 4, 6
and 8, followed by three repeat waves each at 4, 5 and 6. Five concurrent calls
completed 15/15 pages in the repeat test at 5.95--9.71 pages/minute. Seven
transient HTTP 503 responses recovered automatically and no HTTP 429 occurred.
Six completed only 17/18 repeat pages, while eight produced unstable four-minute
tail latency. Production is therefore capped at five total concurrent requests,
not five per House. Raw results are stored in the dated
`nvidia-concurrency*.json` files beside this report.

## Live API validation

Two existing flagged pages were used before session-scale review:

- A 767-character table page completed successfully in about 14 seconds and
  was stored as an NVIDIA adjudication.
- A dense 7,382-character table page exceeded the 240-second read timeout. It
  was not inserted as an adjudication. Its failed request remains diagnostic
  evidence. Dense pages may require region/table chunking or a revised timeout;
  this pilot does not pretend the failure is a transcription.

No HTTP 429 was observed in these sequential Nemotron calls. This is not proof
of an unlimited quota. The adaptive retry and durable checkpoint behavior
remain mandatory because NVIDIA does not publish a fixed quota for this trial
endpoint.

## Reproducible commands

```bash
# Refresh official inventories at the publication cutoff.
.venv/bin/sansad-pipeline census-questions \
  --lok-sabha 18 --session 8 --limit-per-session 0 \
  --page-size 500 --workers 1
.venv/bin/sansad-pipeline census-rs-questions \
  --session 271 --limit-per-session 0 --workers 1

# Acquire each scope separately. Logs are retained under data/.
.venv/bin/sansad-pipeline acquire-census \
  --house lok_sabha --lok-sabha 18 --session 8 --limit 0 --source current \
  --workers 4 --min-free-gib 6.0
.venv/bin/sansad-pipeline acquire-census \
  --house rajya_sabha --session 271 --limit 0 --source current \
  --workers 4 --min-free-gib 6.0
# If the documented 6 GiB gate stops at the observed checkpoint, resume the
# known remainder using the post-audit floor:
.venv/bin/sansad-pipeline acquire-census \
  --house rajya_sabha --session 271 --limit 0 --source current \
  --workers 4 --min-free-gib 5.5

# Process only the named scope; successful identical runs are reused.
.venv/bin/sansad-pipeline process-scope \
  --house lok_sabha --parliament 18 --session 8 --workers 2
.venv/bin/sansad-pipeline process-scope \
  --house rajya_sabha --parliament '' --session 271 --workers 2

# Inspect checkpointed progress at any time.
.venv/bin/sansad-pipeline scope-status \
  --house lok_sabha --parliament 18 --session 8

# Transcribe every page with Nemotron, one request at a time, resumably.
.venv/bin/sansad-pipeline adjudicate-scope-nvidia \
  --house lok_sabha --parliament 18 --session 8 --workers 3
.venv/bin/sansad-pipeline adjudicate-scope-nvidia \
  --house rajya_sabha --parliament '' --session 271 --workers 2

# Build and verify separate, bounded publication tranches.
.venv/bin/sansad-pipeline prepare-publication \
  --house lok_sabha --parliament 18 --session 8 \
  --output data/publications/ls18-session8-2026-08-12
.venv/bin/sansad-pipeline verify-publication \
  data/publications/ls18-session8-2026-08-12
.venv/bin/sansad-pipeline prepare-publication \
  --house rajya_sabha --parliament '' --session 271 \
  --output data/publications/rs-session271-2026-08-12
.venv/bin/sansad-pipeline verify-publication \
  data/publications/rs-session271-2026-08-12
```

## Research formats and semantics

Each tranche contains original official PDFs and, when qpdf saves at least 5%,
a separately checksummed lossless optimization. Researchers receive:

- canonical and local Markdown;
- plain text;
- per-document JSON;
- Zstandard JSONL for streaming;
- document- and page-level Parquet;
- DuckDB for immediate SQL analysis;
- a WebDataset TAR for large-scale sequential access;
- `SHA256SUMS` and tranche metadata.

On reviewed pages, canonical fields contain the latest stored adjudication and
`local_*` fields retain the local candidate. Provider, model and request hash
are included. Unreviewed pages use local output as canonical output. “Canonical”
means the selected reproducible candidate; it does not mean human-verified.

## Completion checklist

- [x] Refresh LS and RS official inventories.
- [x] Estimate disk use and establish a safety floor.
- [x] Implement and live-test sequential Nemotron review with retries.
- [x] Preserve model review provenance in publication formats.
- [ ] Finish LS acquisition and record exact bytes/failures.
- [ ] Finish RS acquisition and record exact bytes/failures.
- [ ] Process both scopes and report routes/validation flags.
- [ ] Review flagged pages; report successes, failures and latency/rate limits.
- [ ] Build and checksum both tranches.
- [ ] Upload both tranches to `bebhuvan1/sansad-corpus` and record commit URLs.
