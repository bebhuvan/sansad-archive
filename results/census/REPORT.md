# Questions census and acquisition report

Run date: 2026-08-01 (Asia/Kolkata)

## Inventory

| Source | House | Records | Distinct source URLs | Date range |
|---|---:|---:|---:|---|
| Parliament eLibrary Q&A collection | Lok Sabha | 1,155,268 | 1,155,268 | 1952-05-19 to 2026-04-02 |
| Current Digital Sansad API | Lok Sabha | 179,089 | 179,050 | 2000-02-25 to 2026-07-31 |
| Current Digital Sansad/RS document API | Rajya Sabha | 258,987 | 248,529 | 2001-11-19 to 2026-07-30 |
| **All source records** | | **1,593,344** | **1,582,847** | 1952-05-19 to 2026-07-31 |

These are source records, not a claim of 1,593,344 byte-distinct PDFs. The
content-addressed store deduplicates downloaded files by SHA-256. Overlap between
the eLibrary and current Lok Sabha API is intentionally retained as provenance.

The eLibrary audit is exact: all 11,553 API pages completed and the number of
distinct stored eLibrary UUIDs equals the collection's reported `totalElements`
of 1,155,268.

The current Rajya Sabha API completed all 73 sessions it exposes (194–271, with
official gaps in the session list), with no failed scopes. The current Lok Sabha
API completed 76 of 84 exposed Lok Sabha/session scopes. Eight older scopes
return persistent HTTP 500 responses: Lok Sabha 13 sessions 8–14 and Lok Sabha
15 session 10. Those dates are covered by the complete eLibrary inventory.

## Storage

A 12-point evenly distributed sample of original eLibrary bitstreams measured:

- minimum: 20,381 bytes;
- median: 202,982 bytes;
- mean: 259,241 bytes;
- maximum: 1,061,303 bytes.

Mean × collection size projects about 299.5 GB (278.9 GiB) for the Lok Sabha
eLibrary originals alone. The machine had about 60.7 GiB free, so an unbounded
download is unsafe. A subsequent 20-record acquisition added 4,083,144 bytes;
all 20 hashes were distinct. The acquisition command therefore defaults to a
10 GiB free-space floor and requires an explicit `--limit 0` for an unbounded
run.

## Historical extraction

The eLibrary PDF tested from 25 November 1959 contains a full-page 350-DPI scan
plus an old OCR text layer. Native LiteParse accepted that text layer, which is
not independent digitization. The production router now detects
`full_page_image`, renders the page in memory, and forces fresh English OCR at
250 DPI. Ordinary OCR remains at the previously benchmarked 150 DPI.

For the 1959 page, fresh-OCR mean confidence improved from 0.922 at 150 DPI to
0.940 at 250 DPI with high-quality raster preservation. It correctly retained
the header year and exposed a decimal-like value (`4:19 acres`) that the old
layer had collapsed to `419`. Numeric disagreement and table-width checks kept
the page in review.

Three May 1952 samples produced fresh-OCR mean confidence of roughly 0.92–0.94.
All remained in review because multi-column/table reconstruction and numeric
disagreement are not archival-safe to auto-accept.

## Vision review

Qwen3.7 Flash successfully adjudicated a flagged 1952 page. The English-only
prompted repeat omitted the page's Hindi block instead of translating it and
cost $0.00094281. The canonical JSONL export now chooses the latest stored
adjudication for reviewed pages and preserves the local candidate, model,
tokens, cost, and response path.

Across all live model tests so far, seven successful OpenRouter calls cost
$0.00479945. Vision remains review-only; it is not used for every page.

## Verified smoke paths

- 1952 and 1959 Lok Sabha archival scans: forced fresh OCR and conservative review.
- Current Lok Sabha PDF: native-first production path.
- Current Rajya Sabha Session 271 PDF: four native pages, all accepted.
- Parallel identical ingestion: one immutable SHA-256 target under concurrent workers.
- Test suite: 12 tests passing; dependency check clean.
