# Quality, validation, and testing

## Meaning of `accepted`

An accepted page passed current automated checks. It does not mean a human
verified every character or that tables and numbers are archival-grade. OCR
prose was often good while observed runs included `14.2` becoming `142` and
`20.59` becoming `2059`.

The policy is therefore to preserve candidates and escalate disagreement—not
to assume native text, OCR confidence, or a vision model is truth.

## Routing and validation

OCR routing conditions are a full-page image, empty or too-short native text,
low alphanumeric ratio, or a configured LiteParse complexity reason. Full-page
images get independent fresh OCR at 250 DPI even with an old text layer;
ordinary OCR uses 150 DPI. `sparse-text` alone is insufficient.

Review flags are:

- empty output;
- Unicode replacement characters above threshold;
- low mean OCR confidence;
- inconsistent Markdown table widths;
- different normalized numeric-token multisets in native and OCR candidates.

Numeric comparison is intentionally sensitive. It may flag benign layout
differences, but prevents quiet decimal loss from auto-acceptance.

## Secondary evidence

`pdf-inspector` compares native extraction and numeric tokens. PaddleOCR can be
configured as an external engine for a fresh run. OpenRouter vision adjudicates
only selected review pages. Agreement is evidence, not truth—particularly if
two engines both consume the same embedded text layer.

Human review should prioritize question/date/member/ministry fields, decimal
values, units, totals, footnotes, and table row/column association.

## Current evidence

- Recent born-digital Lok Sabha and Rajya Sabha samples succeeded natively.
- A 1959 full-page scan with old OCR improved from about 0.922 mean confidence
  at 150 DPI to 0.940 at 250 DPI, but stayed in review for numeric/table issues.
- Three May 1952 samples scored about 0.92–0.94 and stayed in review.
- Seven successful OpenRouter trials reported $0.00479945 total. An English-only
  Qwen3.7 repeat cost $0.00094281 and omitted Hindi as instructed.

See `REPORT.md`, `results/census/REPORT.md`, and
`results/openrouter_qwen_trial/REPORT.md`. These are feasibility trials, not a
representative accuracy study.

## Tests and benchmarks

```bash
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/pip check
.venv/bin/python scripts/benchmark_liteparse.py --resume
.venv/bin/python scripts/evaluate_openrouter_trial.py
```

At the 2026-08-01 checkpoint, 12 unit tests passed and dependency checks were
clean. Tests cover eLibrary normalization/page caps, Rajya Sabha co-asker
grouping, routing/validation, OpenRouter fallback, and concurrent immutable
storage. They intentionally do not exercise live government APIs by default.

## Work required before publication

Build a human-transcribed gold set stratified by house, decade, source,
scan/native route, layout, and subtype. Measure prose character/word error,
structured-field exact match, numeric precision/recall and decimal retention,
table cell association, routing false accepts, review rate, and adjudication
accuracy/cost. Choose thresholds from those measurements.

Release gate:

1. Freeze and hash census/configuration.
2. Record parser, OCR, model, and code versions.
3. Run tests and stratified evaluation.
4. Report accepted, reviewed, model-adjudicated, and human-verified counts
   separately.
5. Publish source gaps and confidence limitations.
6. Retain PDF hash, URL, run, page, and canonical provenance for corrections.

