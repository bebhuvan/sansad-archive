#!/usr/bin/env python3
"""Classify a session run after scope status.

A scope whose census returned zero records is deterministically unavailable in
the current API (typically a pre-2000 session). Mark it skipped so the nightly
batch does not retry it forever, and let the workflow skip publication.
Prints a GitHub Actions step output line: empty=true|false.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    status_path = Path("/tmp/scope-status.json")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("empty=false")
        return 0
    empty = int(status.get("records") or 0) == 0
    print(f"empty={'true' if empty else 'false'}")
    if empty:
        key = (
            f"{os.environ.get('HOUSE', '')}-p{os.environ.get('PARLIAMENT', '')}"
            f"-s{os.environ.get('SESSION', '')}"
        )
        payload = {
            "house": os.environ.get("HOUSE"),
            "parliament": os.environ.get("PARLIAMENT"),
            "session": os.environ.get("SESSION"),
            "reason": "census returned zero records for this scope",
            "skipped_at": datetime.now(timezone.utc).isoformat(),
            "scope_status": status,
        }
        Path(f"/tmp/session-skipped-{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
