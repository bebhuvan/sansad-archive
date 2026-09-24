from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventLog:
    """Append-only JSONL event stream with a human-readable stdout mirror.

    Every record carries a UTC timestamp and an event name. Fields are kept
    flat so the stream can be filtered with jq or loaded into SQLite/DuckDB.
    The file is the durable evidence; stdout is for operators.
    """

    def __init__(self, path: Path | None = None, *, stream=None):
        self.path = path
        self.stream = stream if stream is not None else sys.stdout
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()

    def emit(self, event: str, **fields) -> dict:
        record = {"ts": utcnow(), "event": event, **fields}
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        print(self._line(record), file=self.stream, flush=True)
        return record

    @staticmethod
    def _line(record: dict) -> str:
        parts = [f"[{record['ts']}] {record['event']}"]
        for key, value in record.items():
            if key in {"ts", "event"}:
                continue
            text = str(value)
            if len(text) > 160:
                text = text[:157] + "..."
            parts.append(f"{key}={text}")
        return " ".join(parts)
