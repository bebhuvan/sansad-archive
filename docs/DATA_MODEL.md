# Data model and exports

State is SQLite at `data/pipeline.sqlite3`, using WAL, foreign keys, and a
30-second busy timeout. Initialization is idempotent and detects/migrates one
legacy census uniqueness layout.

```text
documents 1---* sources
    |
    +---* runs 1---* pages
                      +---* adjudications (run/page)

census_runs 1---* census_scopes
census_records *---0..1 documents (after acquisition)
```

## Tables and statuses

### `documents`

One byte-distinct PDF keyed by SHA-256. Records size, media type, immutable file
path, and admission time. PDF blobs are not stored in SQLite.

### `sources`

One provenance observation, unique by `(document_sha256, source_uri)`, retaining
original name, acquisition time, and metadata. One hash can have many URLs.

### `runs`

One extraction attempt with document hash, exact config JSON, artifact directory,
timestamps, and error/traceback. Statuses: `running`, `complete`, `failed`.
Interrupted processes may leave `running`; only `complete` is reused/exported.

### `pages`

One summary per `(run_id, page_number)`: `native`/`ocr` route, engine, character
count, confidence, `accepted`/`review`, flags, and full artifact path. The page
JSON contains text, Markdown, geometry, complexity, routing reasons, engine
version, document hash, and run ID.

### `adjudications`

Successful paid results with provider/model, request hash, raw response path,
tokens, provider-reported cost, and time. They never mutate pages. Multiple can
exist; export selects the greatest ID for a run/page.

### `census_runs`

Discovery invocations with source/scope, observed count, timestamps, and error.
Statuses: `running`, `complete`, `partial`, `failed`, `interrupted`.

### `census_scopes`

Session/page checkpoints keyed by `(run_id, house, parliament_number, session)`.
Statuses include `pending`, `running`, `complete`, `failed`, `interrupted`.

### `census_records`

Official records keyed by normalized `record_id`. Fields include source, house,
parliament/session, document number/subtype/date, title, ministry, members,
language, source/official/API URLs, API params, minimized raw JSON, and discovery
time. Acquisition states:

- `discovered`: normal acquisition queue;
- `downloaded`: linked to `document_sha256`;
- `failed`: error retained; selected only with `--retry-failed`.

Different records can share URLs or hashes.

## Export semantics

`export-census` streams every census record, supporting `.jsonl` and `.jsonl.gz`.

`export-jsonl` selects the latest completed run per document:

| Options | Pages | Canonical selection |
|---|---|---|
| none | accepted + review | no canonical fields |
| `--accepted-only` | accepted | no canonical fields |
| `--with-adjudications` | all | latest adjudication, else local |
| both | accepted + adjudicated review | latest adjudication, else local |

Publication format 1.1 applies the same selection explicitly: `text` and
`markdown` are canonical fields, while `local_text` and `local_markdown` always
retain the local parser candidate. Reviewed pages also carry provider, model and
request SHA-256. Canonical means reproducibly selected, not human-verified.

Canonical means deterministic selection, not certified truth. Publications
should retain document SHA, run, page, and `canonical_source`.

The 2026-08-01 census database was about 2.48 GB. Back up before migrations.
Large deletion/vacuum is not part of normal operations.
