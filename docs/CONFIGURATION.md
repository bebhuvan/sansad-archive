# Configuration reference

`pipeline.toml` is loaded before every command. Relative storage paths resolve
from that file's directory. `--config` must precede the subcommand.

## Settings

| Section/key | Current value | Meaning |
|---|---:|---|
| `storage.root` | `data` | Database, raw store, artifacts, temp, exports |
| `liteparse.version` | `2.10.1` | Pinned/recorded parser version |
| `liteparse.language` | `eng` | English-only OCR policy |
| `liteparse.dpi` | `150` | Ordinary OCR resolution |
| `liteparse.full_page_image_dpi` | `250` | Independent OCR for full-page scans |
| `liteparse.workers` | `4` | Internal workers per document |
| `liteparse.max_pages` | `10000` | Per-document page safety ceiling |
| `liteparse.ocr_server_url` | empty | Optional external OCR endpoint |
| `liteparse.keep_headers_footers` | `false` | LiteParse header/footer option |
| `liteparse.preserve_small_text` | `false` | LiteParse small-text option |
| `routing.minimum_native_characters` | `40` | Shorter native output routes to OCR |
| `routing.minimum_alphanumeric_ratio` | `0.35` | Lower visible-text ratio routes to OCR |
| `routing.force_ocr_full_page_images` | `true` | Image-only fresh OCR for scans |
| `routing.ocr_reasons` | scanned, no-text, garbled, vector-text, annotation-text | Complexity reasons sufficient for OCR |
| `routing.ignore_reasons` | sparse-text, embedded-images | Policy documentation; see note below |
| `validation.minimum_ocr_confidence` | `0.70` | Lower mean confidence flags review |
| `validation.maximum_replacement_characters` | `0` | Allowed Unicode replacement characters |
| `validation.flag_numeric_disagreement` | `true` | Compare OCR/native numeric multisets |
| `validation.flag_inconsistent_table_width` | `true` | Flag unequal Markdown table widths |
| `openrouter.enabled` | `true` | Permit explicit adjudication command |
| `openrouter.endpoint` | OpenRouter chat completions | Model endpoint |
| `openrouter.models_endpoint` | OpenRouter models API | Capability/pricing metadata |
| `nvidia.endpoint` | NVIDIA hosted chat completions | Free-trial Nemotron endpoint |
| `nvidia.model` | Nemotron 3 Nano Omni reasoning | Stored with every adjudication |
| `nvidia.minimum_interval_seconds` | `3.0` | Minimum delay between completed calls |
| `nvidia.max_retries` | `6` | Retry ceiling for 429/transient HTTP failures |
| `nvidia.maximum_backoff_seconds` | `45.0` | Backoff ceiling; `Retry-After` is honored |
| `nvidia.max_concurrency` | `5` | Measured global production ceiling; split across Houses |
| `openrouter.model` | `stealth/space-bunny-alpha` | Single-model default |
| `openrouter.models` | Space Bunny Alpha | Ordered fallback list |
| `openrouter.max_tokens` | `8192` | Completion ceiling per page |
| `openrouter.timeout_seconds` | `300` | HTTP timeout |
| `openrouter.image_dpi` | `150` | Page render sent to vision model |
| `openrouter.max_pages_per_command` | `200` | Model-page ceiling per invocation |
| `openrouter.max_cumulative_cost_usd` | `10.0` | Stop after stored reported spend reaches it |
| `openrouter.reasoning_effort` | `low` | Reasoning budget; Space Bunny requires reasoning and rejects `none` |
| `openrouter.reasoning_exclude` | `true` | Keep hidden reasoning out of the stored adjudication |
| `openrouter.store_reasoning` | `false` | Also save the reasoning text beside the response |
| `openrouter.minimum_interval_seconds` | `2.0` | Minimum delay between completed calls |
| `openrouter.max_retries` | `6` | Retry ceiling for 429/transient HTTP failures |
| `openrouter.maximum_backoff_seconds` | `60.0` | Backoff ceiling; `Retry-After` is honored |
| `openrouter.max_concurrency` | `3` | Ceiling for `adjudicate-scope-openrouter --workers` |

Changing LiteParse, routing, or validation settings changes the config snapshot
and causes a new run. Validation flags review; it never corrects text.

`ignore_reasons` is loaded and recorded, but routing currently works by
intersection with `ocr_reasons`. To ignore a reason, ensure it is absent from
`ocr_reasons`; adding it only to `ignore_reasons` does not change behavior.

## Environment

The process environment is loaded first; `.env` fills only missing variables.

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | OpenRouter inference secret |
| `OPENROUTER_MODELS` | Comma-separated ordered list overriding TOML |
| `OPENROUTER_MODEL` | Single model when a model list is not selected |
| `OPENCODE_API_KEY` | OpenCode Go key for `scripts/verify_pages.py`; falls back to the local OpenCode auth file |
| `HF_TOKEN` | Hugging Face write token for `publish-hf` and cloud checkpoints |
| `GITHUB_SHA` / `PIPELINE_COMMIT` | Commit recorded in publication metadata when present |

`adjudicate --model provider/model` forces one model and disables fallback for
that command. Never commit `.env`; use `.env.example` as the template.

`--limit 0` means all records where the command documents that behavior.
`--force` means deliberate recomputation/repeat payment, not “retry failures.”
