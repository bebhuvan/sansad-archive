# Sources, coverage, and provenance

## Implemented sources

### Parliament eLibrary — Lok Sabha Q&A

- Base: `https://elibrary.sansad.in`
- Discovery: `/server/api/discover/search/objects`
- Collection UUID: `75228d43-3a98-4b7d-b90f-ffec6aa9fa11`
- Collection page: `https://elibrary.sansad.in/collections/75228d43-3a98-4b7d-b90f-ffec6aa9fa11`

Discovery stores item metadata. Acquisition resolves the `ORIGINAL` bundle and
downloads the PDF, ignoring supplied OCR/digitized text. DSpace caps pages at
100; the connector rejects larger values.

### Current Digital Sansad — Lok Sabha

- API: `https://sansad.in/api_ls`
- Human page: `https://sansad.in/ls/questions/questions-and-answers`
- Parliament/session list: `/business/getAllLoksabhaAndSession?locale=en`
- Search: `/question/qetFilteredQuestionsAns`

### Current Digital Sansad/RS document service — Rajya Sabha

- API: `https://sansad.in/api_rs`
- Session list: `/business/getSessionsList?docType=SQ`
- Search: `https://rsdoc.nic.in/Question/Search_Questions`
- Human page: `https://sansad.in/rs/questions/questions-and-answers`

The RS connector chooses English `files` and groups duplicate co-asker rows
while retaining member metadata.

## Measured snapshot: 2026-08-01

| Source | House | Records | Distinct URLs | Dates |
|---|---:|---:|---:|---|
| eLibrary Q&A | Lok Sabha | 1,155,268 | 1,155,268 | 1952-05-19–2026-04-02 |
| Current API | Lok Sabha | 179,089 | 179,050 | 2000-02-25–2026-07-31 |
| Current RS APIs | Rajya Sabha | 258,987 | 248,529 | 2001-11-19–2026-07-30 |
| **Total** | | **1,593,344** | **1,582,847** | 1952-05-19–2026-07-31 |

These are source records, not distinct PDFs. Overlap is retained until hashing.
The eLibrary audit completed 11,553 pages and exactly matched its reported total.
Rajya Sabha completed all 73 exposed sessions (194–271, with official gaps).

Current Lok Sabha completed 76/84 scopes. Eight consistently returned HTTP 500:
Lok Sabha 13 sessions 8–14 and Lok Sabha 15 session 10. eLibrary covers those
dates, but this does not make the current API complete.

Snapshot artifact:

- `data/exports/census-2026-08-01.jsonl.gz`
- HF mirror: `bebhuvan1/sansad-corpus/state/census/snapshot-2026-08-01.jsonl.gz`
  with `snapshot-2026-08-01.json` manifest (remote SHA-256 verified 2026-09-24)
- 1,593,344 lines; 310 MB
- SHA-256 `f0cec93b7f078af545a4b6a1647b50fdae4e90d769172b6bfd922481dbeb2b0e`

## Storage evidence

A 12-record distributed eLibrary sample measured 20,381-byte minimum,
202,982-byte median, 259,241-byte mean, and 1,061,303-byte maximum. Mean times
collection size projects 299.5 GB/278.9 GiB for this Lok Sabha collection alone.
This is an estimate, not a confidence bound. The machine had about 60.7 GiB free.
A later 20-record acquisition added 4,083,144 bytes; all hashes were distinct.

## Language, freshness, and exclusions

The project requests, extracts, and adjudicates English only. It does not
translate Hindi; mixed pages instruct the model to omit Hindi.

Official sites change. Any “all PDFs” claim must include a fresh census date,
hashed export, comparison to the prior snapshot, and disclosed gaps.

Not currently inventoried or downloaded:

- Lok Sabha/Rajya Sabha debates and proceedings;
- standing committee reports or transcripts;
- other parliamentary paper collections;
- Hindi-only PDFs;
- third-party mirrors or digitizations.
