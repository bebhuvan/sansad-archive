# OpenRouter Qwen vision trial

Test date: 2026-08-01

## Setup

The same page image and local LiteParse candidate were sent to:

- `qwen/qwen3.7-flash`
- `qwen/qwen3.5-9b`

Test pages:

- One manually transcribed page from an official 1985 Indian government scan.
- Pages 2 and 3 of a controlled image-only version of a modern Lok Sabha table.
  The born-digital original supplies the table-cell reference.

No full document was uploaded. Each request contained one locally rendered page.

## Results

| Page | Engine | Word F1 | Numeric F1 | Exact table-cell accuracy | Outcome |
|---|---|---:|---:|---:|---|
| 1985 prose p3 | LiteParse OCR | 0.9865 | 1.0000 | n/a | Success |
| 1985 prose p3 | Qwen3.7 Flash | 0.9933 | 1.0000 | n/a | Success |
| 1985 prose p3 | Qwen3.5-9B | 0.9933 | 1.0000 | n/a | Success |
| Controlled table p2 | LiteParse OCR | 0.9740 | 0.9641 | 92.00% | Success |
| Controlled table p2 | Qwen3.7 Flash | n/a | n/a | n/a | Provider moderation rejection |
| Controlled table p2 | Qwen3.5-9B | 0.9792 | 0.9921 | **100%** | Success |
| Controlled table p3 | LiteParse OCR | 0.9455 | 0.9739 | 56.94% | Success |
| Controlled table p3 | Qwen3.7 Flash | **1.0000** | **1.0000** | **100%** | Success |
| Controlled table p3 | Qwen3.5-9B | **1.0000** | **1.0000** | **100%** | Success |

The lower word/numeric F1 for Qwen3.5 on table page 2 is caused by additional
correct heading metadata visible in the image but absent from LiteParse's native
Markdown reference. Every reference table cell was exact.

Both Qwen outputs included `Copy No. IV` on the historical page. That text is
visible in the image but was omitted from the manual gold transcription. The
substantive body and all numeric tokens were exact.

## Cost

| Model | Successful pages | Failed pages | Total tokens | Provider-reported cost |
|---|---:|---:|---:|---:|
| Qwen3.7 Flash | 2 | 1 | 13,033 | $0.00114369 |
| Qwen3.5-9B | 3 | 0 | 15,746 | $0.00192660 |
| **Total** | **5** | **1** | **28,779** | **$0.00307029** |

The rejected Qwen3.7 request returned HTTP 400 with Alibaba error
`data_inspection_failed`, claiming that an official government biogas table
might contain inappropriate content. No charge was reported for that failure.

## Decision

Qwen3.5-9B is the more reliable table adjudicator in this tiny trial: it was
perfect at the table-cell level on both pages and completed every request.
Qwen3.7 Flash was also perfect when it responded and was cheaper, but the false
moderation rejection makes it unsafe as the sole fallback.

Use local native extraction as the default, local OCR for scanned pages, and a
Qwen model only for validation failures. A sensible production order to test on
a larger gold set is Qwen3.7 first with automatic retry/fallback to Qwen3.5-9B,
or Qwen3.5 directly for high-value numeric tables.

Machine-readable measurements are in `evaluation.json`.

