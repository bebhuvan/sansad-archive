from __future__ import annotations

import base64
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
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import Config
from .openrouter import load_dotenv, utcnow
from .storage import Store


NVIDIA_OCR_PROMPT = """Transcribe this parliamentary document page exactly into Markdown.
Transcribe only English-language content. Omit Hindi and other non-English
blocks; do not translate them. Preserve reading order, headings,
question/answer labels, table rows and columns, punctuation, decimal points,
signs, and footnotes. Never calculate, normalize, summarize, or silently repair
a number. If a character or cell is illegible, use [ILLEGIBLE] instead of
guessing. Return only the transcription Markdown."""


class NvidiaHTTPError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(f"NVIDIA HTTP {status}: {detail[:1000]}")


class NvidiaAdjudicator:
    """Rate-limited NVIDIA NIM reviewer for locally flagged pages."""

    def __init__(self, config: Config, *, sleep=time.sleep, random_value=random.random):
        self.config = config
        self.store = Store(config)
        self.store.initialize()
        load_dotenv(config.project_root / ".env")
        self._sleep = sleep
        self._random = random_value
        self._last_request_finished = 0.0

    def _latest_run(self, identifier: str):
        document = self.store.document(identifier)
        run = self.store.db.one(
            "SELECT * FROM runs WHERE document_sha256=? AND status='complete' ORDER BY id DESC",
            (document["sha256"],),
        )
        if not run:
            raise RuntimeError("process the document locally before adjudicating it")
        return document, run

    def _page_rows(self, run_id: int, pages: list[int] | None, *, all_pages: bool = False):
        if pages is None:
            if all_pages:
                return self.store.db.all(
                    "SELECT * FROM pages WHERE run_id=? ORDER BY page_number",
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

    def _render_page(self, pdf: Path, page_number: int, output_dir: Path) -> Path:
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
                "--target-pages", str(page_number), "--dpi",
                str(self.config.nvidia.image_dpi), "--quiet",
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
        cfg = self.config.nvidia
        retryable = {408, 429, 500, 502, 503, 504}
        events = []
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
                            if key.casefold() in {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
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
                    "retry_wait_seconds": wait if error.code in retryable and attempt < cfg.max_retries else None,
                    "detail": detail[:10000],
                })
                log_path.write_text(json.dumps(events, indent=2), encoding="utf-8")
                self._last_request_finished = time.monotonic()
                if error.code not in retryable or attempt >= cfg.max_retries:
                    raise NvidiaHTTPError(error.code, detail) from error
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
                    raise RuntimeError(f"NVIDIA request failed: {reason}") from error
                self._sleep(wait)
        raise AssertionError("unreachable")

    def adjudicate(
        self,
        identifier: str,
        *,
        pages: list[int] | None = None,
        all_pages: bool = False,
        force: bool = False,
    ) -> dict:
        cfg = self.config.nvidia
        if not cfg.enabled:
            raise RuntimeError("NVIDIA is disabled; set nvidia.enabled = true in pipeline.toml")
        api_key = os.environ.get("NVIDIA_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("NVIDIA_API_KEY is missing; add it to .env")
        document, run = self._latest_run(identifier)
        page_rows = self._page_rows(int(run["id"]), pages, all_pages=all_pages)
        if len(page_rows) > cfg.max_pages_per_command:
            raise RuntimeError(
                f"refusing {len(page_rows)} pages; limit is {cfg.max_pages_per_command} per command"
            )
        summary: dict = {"provider": "nvidia", "model": cfg.model, "completed": [], "skipped": [], "failed": []}
        model_dir = re.sub(r"[^a-zA-Z0-9._-]+", "__", cfg.model)
        destination = Path(run["artifact_dir"]) / "nvidia" / model_dir
        for page_row in page_rows:
            number = int(page_row["page_number"])
            if not force and self.store.db.one(
                """SELECT id FROM adjudications WHERE run_id=? AND page_number=?
                   AND provider='nvidia' AND model=? ORDER BY id DESC LIMIT 1""",
                (run["id"], number, cfg.model),
            ):
                summary["skipped"].append({"page": number, "reason": "already complete"})
                continue
            attempt_id = utcnow().replace(":", "").replace("+", "_")
            page_dir = destination / f"page-{number:05d}" / f"attempt-{attempt_id}"
            page_dir.mkdir(parents=True, exist_ok=True)
            image = self._render_page(Path(document["raw_path"]), number, page_dir)
            mime = "image/png" if image.suffix.casefold() == ".png" else "image/jpeg"
            image_url = f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode('ascii')}"
            payload = {
                "model": cfg.model,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": NVIDIA_OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }],
                "temperature": 0,
                "max_tokens": cfg.max_tokens,
                "reasoning_budget": cfg.reasoning_budget,
                "stream": False,
            }
            request_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            request_digest = hashlib.sha256(request_bytes).hexdigest()
            request = urllib.request.Request(
                cfg.endpoint,
                data=request_bytes,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "SansadArchive/0.1",
                },
                method="POST",
            )
            try:
                response_payload, response_headers = self._post(request, page_dir / "request-attempts.json")
                message = response_payload.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not message.strip():
                    raise RuntimeError("NVIDIA returned an empty transcription")
                response_path = page_dir / "response.json"
                response_path.write_text(json.dumps(response_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                (page_dir / "adjudicated.md").write_text(message, encoding="utf-8")
                (page_dir / "provenance.json").write_text(
                    json.dumps({
                        "provider": "nvidia",
                        "endpoint": cfg.endpoint,
                        "model": cfg.model,
                        "request_sha256": request_digest,
                        "image_dpi": cfg.image_dpi,
                        "reasoning_budget": cfg.reasoning_budget,
                        "response_rate_limit_headers": {
                            key: value for key, value in response_headers.items()
                            if key.casefold() in {"retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"}
                        },
                        "created_at": utcnow(),
                    }, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                usage = response_payload.get("usage") or {}
                self.store.db.execute(
                    """INSERT INTO adjudications
                       (run_id,page_number,provider,model,request_sha256,response_path,
                        prompt_tokens,completion_tokens,total_tokens,reported_cost,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run["id"], number, "nvidia", cfg.model, request_digest,
                        str(response_path), usage.get("prompt_tokens"),
                        usage.get("completion_tokens"), usage.get("total_tokens"),
                        usage.get("cost"), utcnow(),
                    ),
                )
                summary["completed"].append({"page": number})
            except Exception as error:
                summary["failed"].append({"page": number, "error": str(error)})
        return summary
