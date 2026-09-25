from __future__ import annotations

import base64
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import Config
from .image_quality import rendered_ink_metrics
from .storage import Store
from .text_quality import local_content_empty


PROMPT = """Transcribe this parliamentary document page exactly into Markdown.
Transcribe only English-language content. Omit Hindi and other non-English
blocks; do not translate them.
Preserve reading order, headings, question/answer labels, table rows and columns,
punctuation, decimal points, signs, and footnotes. Never calculate, normalize,
summarize, or silently repair a number. If a character or cell is illegible, use
[ILLEGIBLE] instead of guessing. Return only the transcription Markdown.

The local parser produced the following candidate. Use the page image as the
authority and correct the candidate where necessary:

--- LOCAL CANDIDATE ---
{candidate}
--- END CANDIDATE ---
"""


BASE_PROMPT = """Transcribe this parliamentary document page exactly into Markdown.
Transcribe only English-language content. Omit Hindi and other non-English
blocks; do not translate them.
Preserve reading order, headings, question/answer labels, table rows and columns,
punctuation, decimal points, signs, and footnotes. Never calculate, normalize,
summarize, or silently repair a number. If a character or cell is illegible, use
[ILLEGIBLE] instead of guessing. Return only the transcription Markdown."""


def local_candidate(local: dict) -> str:
    """Do not offer an empty parser code fence as if it were page content."""
    text = str(local.get("text") or "")
    markdown = str(local.get("markdown") or "")
    return "" if local_content_empty(text, markdown) else markdown


def visual_quality_flags(metrics: dict, local: dict, model_text: str) -> list[str]:
    if not metrics["visually_blank"]:
        return []
    flags = []
    if not local_content_empty(str(local.get("text") or ""),
                               str(local.get("markdown") or "")):
        flags.append("local-nonempty-on-visually-blank-page")
    if model_text.strip():
        flags.append("model-nonempty-on-visually-blank-page")
    return flags


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_page(pdf: Path, page_number: int, output_dir: Path, dpi: int) -> Path:
    lit = Path(sys.executable).with_name("lit")
    if not lit.exists():
        found = shutil.which("lit")
        if not found:
            raise RuntimeError("LiteParse `lit` executable was not found")
        lit = Path(found)
    output_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(lit), "screenshot", str(pdf), "--output-dir", str(output_dir),
            "--target-pages", str(page_number), "--dpi", str(dpi), "--quiet",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    images = sorted(output_dir.glob("*.png")) + sorted(output_dir.glob("*.jpg"))
    if len(images) != 1:
        raise RuntimeError(f"expected one rendered page, found {len(images)} in {output_dir}")
    return images[0]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


class OpenRouterHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"OpenRouter HTTP {status}: {detail[:1000]}")


class OpenRouterRateLimitError(RuntimeError):
    """Every configured model exhausted its retries with HTTP 429."""


class OpenRouterCostViolationError(RuntimeError):
    """The supposedly free provider reported a charge or unparseable cost."""


def checked_reported_cost(usage: dict) -> Decimal | None:
    value = usage.get("cost")
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation as error:
        raise OpenRouterCostViolationError(f"unparseable OpenRouter reported cost: {value!r}") from error
    if not cost.is_finite() or cost != 0:
        raise OpenRouterCostViolationError(f"OpenRouter reported a nonzero or invalid charge: {value!r}")
    return cost


class OpenRouterAdjudicator:
    def __init__(self, config: Config, *, sleep=time.sleep, random_value=random.random):
        self.config = config
        self.store = Store(config)
        self.store.initialize()
        load_dotenv(config.project_root / ".env")
        self._sleep = sleep
        self._random = random_value
        self._last_request_finished = 0.0
        self._snapshot_cache: dict[str, dict] = {}

    def _latest_run(self, identifier: str):
        document = self.store.document(identifier)
        run = self.store.db.one(
            "SELECT * FROM runs WHERE document_sha256=? AND status='complete' ORDER BY id DESC",
            (document["sha256"],),
        )
        if not run:
            raise RuntimeError("process the document locally before adjudicating it")
        return document, run

    def _page_rows(
        self,
        run_id: int,
        pages: list[int] | None,
        *,
        all_pages: bool = False,
        include_ocr: bool = False,
    ):
        if pages is None:
            if all_pages:
                return self.store.db.all(
                    "SELECT * FROM pages WHERE run_id=? ORDER BY page_number",
                    (run_id,),
                )
            if include_ocr:
                return self.store.db.all(
                    """SELECT * FROM pages WHERE run_id=?
                       AND (validation_status='review' OR route='ocr')
                       ORDER BY page_number""",
                    (run_id,),
                )
            return self.store.db.all(
                "SELECT * FROM pages WHERE run_id=? AND validation_status='review' ORDER BY page_number",
                (run_id,),
            )
        if not pages:
            return []
        placeholders = ",".join("?" for _ in pages)
        return self.store.db.all(
            f"SELECT * FROM pages WHERE run_id=? AND page_number IN ({placeholders}) ORDER BY page_number",
            (run_id, *pages),
        )

    def configured_models(self, override: str | None = None) -> list[str]:
        if override:
            return [override]
        env_models = os.environ.get("OPENROUTER_MODELS", "")
        if env_models.strip():
            return [model.strip() for model in env_models.split(",") if model.strip()]
        if self.config.openrouter.models:
            return list(self.config.openrouter.models)
        env_model = os.environ.get("OPENROUTER_MODEL", "").strip()
        model = env_model or self.config.openrouter.model
        return [model] if model else []

    def _page_numbers(
        self,
        identifier: str,
        pages: list[int] | None,
        *,
        all_pages: bool = False,
        include_ocr: bool = False,
    ) -> tuple[int, list[int]]:
        _, run = self._latest_run(identifier)
        rows = self._page_rows(
            int(run["id"]), pages, all_pages=all_pages, include_ocr=include_ocr
        )
        numbers = [int(row["page_number"]) for row in rows]
        if len(numbers) > self.config.openrouter.max_pages_per_command:
            raise RuntimeError(
                f"refusing {len(numbers)} model pages; limit is "
                f"{self.config.openrouter.max_pages_per_command} per command"
            )
        return int(run["id"]), numbers

    def adjudicate_with_fallback(
        self,
        identifier: str,
        *,
        pages: list[int] | None = None,
        model: str | None = None,
        all_pages: bool = False,
        include_ocr: bool = False,
        force: bool = False,
    ) -> dict:
        models = self.configured_models(model)
        if not models:
            raise RuntimeError("no OpenRouter models configured")
        run_id, page_numbers = self._page_numbers(
            identifier, pages, all_pages=all_pages, include_ocr=include_ocr
        )
        summary: dict = {"models": models, "completed": [], "skipped": [], "failed": []}
        for page_number in page_numbers:
            if not force:
                placeholders = ",".join("?" for _ in models)
                existing = self.store.db.one(
                    f"""SELECT model FROM adjudications
                         WHERE run_id=? AND page_number=? AND model IN ({placeholders})
                         ORDER BY id DESC LIMIT 1""",
                    (run_id, page_number, *models),
                )
                if existing:
                    summary["skipped"].append(
                        {"page": page_number, "model": existing["model"], "reason": "already complete"}
                    )
                    continue
            errors = []
            rate_limited = False
            for selected_model in models:
                try:
                    self.adjudicate(
                        identifier,
                        pages=[page_number],
                        model=selected_model,
                        all_pages=all_pages,
                        include_ocr=include_ocr,
                    )
                    summary["completed"].append({"page": page_number, "model": selected_model})
                    break
                except OpenRouterHTTPError as error:
                    errors.append({"model": selected_model, "error": str(error)})
                    if error.status in {401, 402, 403}:
                        raise
                    if error.status == 429:
                        rate_limited = True
                except OpenRouterCostViolationError:
                    raise
                except RuntimeError as error:
                    errors.append({"model": selected_model, "error": str(error)})
            else:
                if rate_limited:
                    raise OpenRouterRateLimitError(
                        f"every configured model was rate limited for page {page_number}"
                    )
                summary["failed"].append({"page": page_number, "attempts": errors})
        return summary

    def _model_snapshot(self, model: str) -> dict:
        cached = self._snapshot_cache.get(model)
        if cached and (
            datetime.now(timezone.utc) - datetime.fromisoformat(cached["checked_at"])
        ).total_seconds() < 60:
            return cached
        request = urllib.request.Request(
            self.config.openrouter.models_endpoint,
            headers={"User-Agent": "SansadArchive/0.1"},
        )
        with urllib.request.urlopen(
            request, timeout=self.config.openrouter.timeout_seconds
        ) as response:
            models = json.loads(response.read().decode("utf-8")).get("data", [])
        snapshot = next((item for item in models if item.get("id") == model), None)
        if snapshot is None:
            raise RuntimeError(f"OpenRouter model not found: {model}")
        modalities = snapshot.get("architecture", {}).get("input_modalities", [])
        if "image" not in modalities:
            raise RuntimeError(
                f"OpenRouter model {model} is text-only and cannot adjudicate page images"
            )
        pricing = snapshot.get("pricing") or {}
        for field in ("prompt", "completion", "image"):
            value = pricing.get(field)
            if field == "image" and value is None:
                continue
            try:
                if value is None or Decimal(str(value)) != 0:
                    raise ValueError
            except (InvalidOperation, ValueError):
                raise RuntimeError(
                    f"OpenRouter model {model} has nonzero or unknown {field} pricing; "
                    "paid calls are disabled"
                )
        result = {
            "id": snapshot.get("id"),
            "canonical_slug": snapshot.get("canonical_slug"),
            "input_modalities": modalities,
            "context_length": snapshot.get("context_length"),
            "pricing": snapshot.get("pricing"),
            "checked_at": utcnow(),
        }
        self._snapshot_cache[model] = result
        return result

    @staticmethod
    def _retry_after(headers, fallback: float) -> float:
        value = headers.get("Retry-After") if headers else None
        if not value:
            return fallback
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return fallback

    def _post(self, request: urllib.request.Request, log_path: Path) -> tuple[dict, dict]:
        cfg = self.config.openrouter
        retryable = {408, 429, 500, 502, 503, 504}
        events: list[dict] = []
        for attempt in range(cfg.max_retries + 1):
            interval_wait = max(
                0.0,
                cfg.minimum_interval_seconds - (time.monotonic() - self._last_request_finished),
            )
            if interval_wait:
                self._sleep(interval_wait)
            started = utcnow()
            try:
                with urllib.request.urlopen(request, timeout=cfg.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    headers = dict(response.headers.items())
                    events.append({
                        "attempt": attempt + 1,
                        "started_at": started,
                        "finished_at": utcnow(),
                        "http_status": response.status,
                        "response_headers": {
                            key: value for key, value in headers.items()
                            if key.casefold() in {
                                "retry-after", "x-ratelimit-limit",
                                "x-ratelimit-remaining", "x-ratelimit-reset",
                            }
                        },
                    })
                    log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
                    self._last_request_finished = time.monotonic()
                    return payload, headers
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                fallback = min(cfg.maximum_backoff_seconds, 2**attempt) + self._random()
                wait = min(
                    cfg.maximum_backoff_seconds,
                    self._retry_after(error.headers, fallback),
                )
                events.append({
                    "attempt": attempt + 1,
                    "started_at": started,
                    "finished_at": utcnow(),
                    "http_status": error.code,
                    "retry_wait_seconds": (
                        wait if error.code in retryable and attempt < cfg.max_retries else None
                    ),
                    "detail": detail[:10000],
                })
                log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
                self._last_request_finished = time.monotonic()
                if error.code not in retryable or attempt >= cfg.max_retries:
                    raise OpenRouterHTTPError(error.code, detail) from error
                self._sleep(wait)
            except (urllib.error.URLError, TimeoutError, socket.timeout) as error:
                wait = min(cfg.maximum_backoff_seconds, 2**attempt) + self._random()
                reason = getattr(error, "reason", error)
                is_timeout = isinstance(error, (TimeoutError, socket.timeout))
                final_attempt = attempt >= cfg.max_retries or (is_timeout and attempt >= 1)
                events.append({
                    "attempt": attempt + 1,
                    "started_at": started,
                    "finished_at": utcnow(),
                    "transport_error": str(reason),
                    "retry_wait_seconds": wait if not final_attempt else None,
                })
                log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
                self._last_request_finished = time.monotonic()
                if final_attempt:
                    raise RuntimeError(f"OpenRouter request failed: {reason}") from error
                self._sleep(wait)
        raise AssertionError("unreachable")

    def _render_page(self, pdf: Path, page_number: int, output_dir: Path) -> Path:
        return render_page(pdf, page_number, output_dir, self.config.openrouter.image_dpi)

    def adjudicate(
        self,
        identifier: str,
        *,
        pages: list[int] | None = None,
        model: str | None = None,
        all_pages: bool = False,
        include_ocr: bool = False,
    ) -> list[int]:
        cfg = self.config.openrouter
        paid_stop = self.config.data_root / "artifacts" / ".openrouter-paid-stop.json"
        if paid_stop.is_file():
            raise OpenRouterCostViolationError(
                f"prior OpenRouter cost violation recorded at {paid_stop}; refusing further calls"
            )
        if not cfg.enabled:
            raise RuntimeError("OpenRouter is disabled; set openrouter.enabled = true in pipeline.toml")
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        selected_model = (model or os.environ.get("OPENROUTER_MODEL") or cfg.model).strip()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is missing; add it to .env")
        if not selected_model:
            raise RuntimeError("set OPENROUTER_MODEL in .env or pass --model")
        model_snapshot = self._model_snapshot(selected_model)
        document, run = self._latest_run(identifier)
        page_rows = self._page_rows(
            int(run["id"]), pages, all_pages=all_pages, include_ocr=include_ocr
        )
        if len(page_rows) > cfg.max_pages_per_command:
            raise RuntimeError(
                f"refusing {len(page_rows)} model pages; limit is {cfg.max_pages_per_command} per command"
            )
        completed: list[int] = []
        model_dir = re.sub(r"[^a-zA-Z0-9._-]+", "__", selected_model)
        destination = Path(run["artifact_dir"]) / "openrouter" / model_dir
        for page_row in page_rows:
            if paid_stop.is_file():
                raise OpenRouterCostViolationError(
                    f"prior OpenRouter cost violation recorded at {paid_stop}; refusing further calls"
                )
            spent = self.store.db.one(
                "SELECT COALESCE(SUM(reported_cost), 0) AS total FROM adjudications"
            )
            total_cost = Decimal(str(spent["total"]))
            ceiling = Decimal(str(cfg.max_cumulative_cost_usd))
            if (ceiling == 0 and total_cost > 0) or (ceiling > 0 and total_cost >= ceiling):
                raise OpenRouterCostViolationError(
                    f"OpenRouter cumulative cost ceiling reached: ${float(spent['total']):.6f} "
                    f">= ${cfg.max_cumulative_cost_usd:.2f}"
                )
            number = int(page_row["page_number"])
            local = json.loads(Path(page_row["artifact_json"]).read_text(encoding="utf-8"))
            attempt_id = utcnow().replace(":", "").replace("+", "_")
            page_dir = destination / f"page-{number:05d}" / f"attempt-{attempt_id}"
            page_dir.mkdir(parents=True, exist_ok=True)
            (page_dir / "model-snapshot.json").write_text(
                json.dumps(model_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            image = self._render_page(Path(document["raw_path"]), number, page_dir)
            visual_quality = rendered_ink_metrics(image)
            mime = "image/png" if image.suffix.casefold() == ".png" else "image/jpeg"
            image_url = f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode('ascii')}"
            candidate = local_candidate(local)
            prompt = PROMPT.format(candidate=candidate) if candidate.strip() else BASE_PROMPT
            payload: dict = {
                "model": selected_model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
                "temperature": 0,
                "max_tokens": cfg.max_tokens,
                "stream": False,
            }
            if cfg.reasoning_effort:
                payload["reasoning_effort"] = cfg.reasoning_effort
            if cfg.reasoning_exclude:
                payload["reasoning"] = {"exclude": True}
            request_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_digest = hashlib.sha256(request_bytes).hexdigest()
            request = urllib.request.Request(
                cfg.endpoint,
                data=request_bytes,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/indianparliament",
                    "X-Title": "Sansad Archive",
                },
                method="POST",
            )
            try:
                response_payload, response_headers = self._post(
                    request, page_dir / "request-attempts.json"
                )
            except (OpenRouterHTTPError, RuntimeError) as error:
                (page_dir / "failure.json").write_text(
                    json.dumps(
                        {
                            "provider": "openrouter",
                            "model": selected_model,
                            "error": str(error),
                            "created_at": utcnow(),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                raise
            message_object = response_payload.get("choices", [{}])[0].get("message", {})
            message = message_object.get("content") or ""
            response_path = page_dir / "response.json"
            response_path.write_text(
                json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not message.strip():
                raise RuntimeError(
                    f"OpenRouter model {selected_model} returned an empty transcription "
                    f"for page {number}"
                )
            finish_reason = response_payload.get("choices", [{}])[0].get("finish_reason")
            if finish_reason in {"length", "content_filter", "error"}:
                raise RuntimeError(
                    f"OpenRouter model {selected_model} ended page {number} with {finish_reason}"
                )
            (page_dir / "adjudicated.md").write_text(message, encoding="utf-8")
            if cfg.store_reasoning and message_object.get("reasoning"):
                (page_dir / "reasoning.md").write_text(
                    str(message_object["reasoning"]), encoding="utf-8"
                )
            (page_dir / "provenance.json").write_text(
                json.dumps(
                    {
                        "provider": "openrouter",
                        "endpoint": cfg.endpoint,
                        "model": selected_model,
                        "request_sha256": request_digest,
                        "image_dpi": cfg.image_dpi,
                        "reasoning_effort": cfg.reasoning_effort or None,
                        "reasoning_excluded": bool(cfg.reasoning_exclude),
                        "visual_quality": visual_quality,
                        "quality_flags": visual_quality_flags(visual_quality, local, message),
                        "response_rate_limit_headers": {
                            key: value for key, value in response_headers.items()
                            if key.casefold() in {
                                "retry-after", "x-ratelimit-limit",
                                "x-ratelimit-remaining", "x-ratelimit-reset",
                            }
                        },
                        "created_at": utcnow(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            usage = response_payload.get("usage") or {}
            try:
                checked_reported_cost(usage)
            except OpenRouterCostViolationError as error:
                paid_stop.parent.mkdir(parents=True, exist_ok=True)
                paid_stop.write_text(json.dumps({
                    "error": str(error), "model": selected_model,
                    "document_sha256": document["sha256"], "page_number": number,
                    "response_path": str(response_path), "created_at": utcnow(),
                }, indent=2), encoding="utf-8")
                raise
            self.store.db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,
                    prompt_tokens,completion_tokens,total_tokens,reported_cost,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run["id"], number, "openrouter", selected_model, request_digest,
                    str(response_path), usage.get("prompt_tokens"), usage.get("completion_tokens"),
                    usage.get("total_tokens"), usage.get("cost"), utcnow(),
                ),
            )
            image.unlink(missing_ok=True)
            completed.append(number)
        return completed
