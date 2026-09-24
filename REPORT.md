# LiteParse 2.10.1: English Sansad PDF trial

## Verdict

LiteParse is good enough to be the cheap first stage of this project. For the
current, born-digital English question-answer PDFs in this sample, native
extraction was extremely fast and preserved even large ruled tables well. Do
not run OCR on every PDF. Route only genuinely image-only or broken-text pages
to OCR, initially at 150 DPI.

It is not yet safe to treat OCR output from old scans as exact data. Prose is
very usable, but table structure and decimal points can fail silently. Retain
the PDF and page image, record the extraction configuration, and apply stricter
validation or a second-stage model only to suspect pages.

## What was tested

- Three official 2026 English Lok Sabha question-answer PDFs: 3, 4, and 41
  pages. The last two contain extensive tables.
- A controlled image-only version of the 4-page table PDF. Its original native
  text provides a reference without assuming external digitization is correct.
- One manually transcribed page from a 42-page official 1985 Indian government
  scan. This is a historical-scan proxy, not a parliamentary document.
- OCR at 100, 150, 200, and 300 DPI; `preserve_very_small_text`; 1, 4, and 8
  workers; header/footer retention; native extraction with OCR disabled.

All runs used pinned `liteparse==2.10.1`, English OCR, Markdown output, images
off, and complexity/vector/text metadata enabled. The complete cases and raw
artifacts are in `results/liteparse_2.10.1/`.

## Results

| Sample and mode | Seconds | Word F1 | Numeric F1 | Markdown table rows |
|---|---:|---:|---:|---:|
| Modern q560, native | 0.039 | reference | reference | 17 |
| Modern q560, OCR enabled at 150 DPI | 2.969 | 1.000 | 1.000 | 17 |
| Modern q1948, native | 0.065 | reference | reference | 51 |
| Modern q1948, OCR enabled at 150 DPI | 3.622 | 1.000 | 1.000 | 51 |
| Controlled scan q1948, 100 DPI | 5.204 | 0.956 | 0.906 | 53 |
| Controlled scan q1948, 150 DPI | 6.574 | **0.987** | 0.972 | **42** |
| Controlled scan q1948, 200 DPI | 8.642 | 0.982 | **0.974** | 37 |
| Controlled scan q1948, 300 DPI | 10.992 | 0.984 | 0.972 | 34 |
| 1985 proxy page, no OCR | 0.002 | 0.000 | 0.000 | 0 |
| 1985 proxy page, 100 DPI | 3.414 | 0.973 | 1.000 | 0 |
| 1985 proxy page, 150 DPI | 4.324 | **0.986** | **1.000** | 0 |
| 1985 proxy page, 200 DPI | 5.081 | 0.973 | 0.870 | 3 (spurious) |
| 1985 proxy page, 300 DPI | 5.627 | 0.975 | 1.000 | 0 |

On modern files, enabling OCR took roughly 45–75 times longer and added no word
or number content. Differences were whitespace/layout only. On the controlled
scan, 150 DPI had the best word score and retained more table structure than
200 or 300 DPI. Higher DPI was slower and was not monotonically more accurate.

The errors matter. At 100 DPI the controlled scan sometimes lost decimal
points (`14.2` became `142`; `20.59` became `2059`). Higher DPI did not eliminate
this class of error. The 1985 proxy was best at 150 DPI; 200 DPI invented a
small table and lost a digit in `204`.

`preserve_very_small_text` made no measurable difference in the tested 300 DPI
cases. Keeping headers/footers changed nothing on the selected long-document
pages. Four OCR workers took 6.6 seconds for the controlled scan, versus 13.5
seconds with one worker and 7.0 seconds with eight; output was identical.

## Important routing trap

LiteParse labelled most modern table-annexure pages `sparse-text` in its
complexity report: 3 of 4 q1948 pages and 40 of 41 q4519 pages. These pages
already had good embedded text, hundreds of vector lines, and coherent tables.
Therefore `needs_ocr == true` or `sparse-text` alone must not trigger expensive
OCR.

Use a page decision rule closer to:

1. Extract native text first with OCR disabled.
2. Accept native extraction when text is non-trivial and not garbled. Vector
   geometry or detected table runs are positive evidence, not reasons to OCR.
3. OCR at 150 DPI when the page has no meaningful native text, is explicitly
   scanned, or fails text-quality checks.
4. Escalate only suspect pages: low OCR confidence, implausible numeric cells,
   broken row widths, high disagreement with a second OCR engine, or failed
   question/answer schema checks.

For search, store both plain page text and Markdown. For auditability, also
store the source URL, SHA-256, page number, parser version/configuration,
complexity reason, OCR confidence, and a pointer to the original PDF/page
image. Never overwrite raw PDFs.

## Corpus count already present in the earlier harness

The existing raw acquisition corpus in `Indica doc parse` currently contains
10,475 official English question-answer PDFs (2.398 GiB): 10,474 Lok Sabha and
one Rajya Sabha. It covers Lok Sabha sessions 6 and 7, so this is a local corpus
count, not a count of all PDFs on both parliamentary sites. It already follows
the desired clean architecture: raw official PDF plus provenance, separate
from later OCR/digitization outputs.

## Limits of this trial

- Only three current parliamentary PDFs were evaluated closely.
- The old manually checked scan is not a parliamentary document and only one
  page was transcribed. We still need a stratified sample of actual pre-1990
  Lok Sabha and Rajya Sabha pages before making a historical accuracy claim.
- Token F1 can hide reading-order errors. Numeric F1 checks whether numeric
  tokens survived, not whether each value remained attached to the correct
  row/column. Table validation must be structural.
- Runtime is local wall-clock time on this machine, not a formal throughput
  benchmark. Cache and process warm-up affect small runs.

## Recommended next experiment

Select roughly 100 pages across decades and page types: prose, two-column
debates, ruled tables, unruled tables, typewritten originals, photocopies,
faint pages, skewed pages, and marginalia. Manually transcribe a smaller but
representative gold subset, especially numeric tables. Compare LiteParse 150
DPI against one independent local OCR engine page by page. Use a vision model
only as an adjudicator for disagreements, not as the bulk extraction engine.

