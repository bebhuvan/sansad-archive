# Roadmap and explicit non-features

This separates planned work from implemented behavior.

## Production hardening

1. Object storage: the cloud path (GitHub Actions compute, Hugging Face
   dataset storage and checkpoints) is implemented in
   [CLOUD_PIPELINE.md](CLOUD_PIPELINE.md). A local storage-backend abstraction
   that preserves SHA paths and atomic admission without a checkout remains
   future work.
2. Add a storage-backend abstraction while preserving SHA paths and atomic
   admission.
3. Budget raw PDFs, database, artifacts, OCR renders, exports, backup, and
   egress; expand the size sample.
4. Build the stratified English gold set and set thresholds empirically.
5. Add a durable human review/correction queue. Do not make model output truth.
6. Add first-class integrity-audit and database-backup commands.

## Corpus expansion

Add separately tested connectors for Lok Sabha debates/proceedings, Rajya Sabha
debates/proceedings, standing committee reports/transcripts, and other paper
collections. Each needs census-only mode, stable IDs, checkpointed pagination,
official provenance, ORIGINAL resolution, bounded acquisition, fixtures, and a
dated completeness report before bulk download.

## Extraction and schema

- Extract house, parliament/session, date, question/type, member, ministry,
  subject, answer, tables, and continuation links only after page text is stable.
- Cross-check fields with census metadata and document invariants.
- Validate table cell geometry, not just Markdown pipe counts.
- Add deterministic candidate consensus and human adjudication history.
- Store the code commit in run provenance.
- Stream page export from SQLite at corpus scale (census export already streams).

## Scale and observability

- Introduce a durable queue and object store for multi-machine work.
- Add host-specific rate limits and throughput/error/retry/review/disk/spend
  metrics.
- Schedule incremental census diffs, including official-record disappearance.
- Package deployment, monitoring, backup, restore, and disaster tests.

## Deferred Hindi scope

Hindi needs separate OCR/model choices, gold data, mixed-script routing,
evaluation, and publication policy. It should not be enabled merely by changing
the `eng` configuration string.

## Completion definition

The archive is not done when every discovered PDF has some text. A defensible
release needs a dated and hashed census, retrievable original bytes,
reproducible provenance, measured accuracy by stratum, unresolved-review counts,
correction history, and a refresh process.

