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


def skip_reason(status: dict) -> str | None:
    records = int(status.get("records") or 0)
    acquired = int(status.get("acquired_documents") or 0)
    acquisition = status.get("acquisition") or {}
    failed = int(acquisition.get("failed") or 0)
    census_complete = status.get("census_status") == "complete"
    if records == 0 and census_complete:
        return "census returned zero records for this scope"
    if (acquired == 0 and records > 0 and failed == records
            and status.get("unsupported_html_records") == records and census_complete):
        return "all current-API originals are HTML, not PDFs; historical eLibrary acquisition is required"
    return None


def publication_ready(status: dict, *, limited: bool, all_pages: bool) -> bool:
    acquisition = status.get("acquisition") or {}
    unsupported_html = int(status.get("unsupported_html_records") or 0)
    pages = int(status.get("pages") or 0)
    return bool(
        status.get("census_status") == "complete"
        and status.get("acquired_documents", 0) > 0
        and status.get("processed_documents") == status.get("acquired_documents")
        and acquisition.get("failed", 0) == unsupported_html
        and (limited or (
            acquisition.get("discovered", 0) == 0
            and acquisition.get("downloaded", 0) + unsupported_html == status.get("records")
        ))
        and all_pages and pages > 0
        and status.get("openrouter_adjudicated_pages", 0) == pages
    )


def main() -> int:
    status_path = Path("/tmp/scope-status.json")
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("empty=false")
        print("publish_ready=false")
        return 0
    reason = skip_reason(status)
    empty = reason is not None
    print(f"empty={'true' if empty else 'false'}")
    ready = publication_ready(
        status, limited=os.environ.get("LIMIT") != "0",
        all_pages=os.environ.get("ALL_PAGES") == "true",
    )
    print(f"publish_ready={'true' if ready else 'false'}")
    if empty:
        key = (
            f"{os.environ.get('HOUSE', '')}-p{os.environ.get('PARLIAMENT', '')}"
            f"-s{os.environ.get('SESSION', '')}"
        )
        payload = {
            "house": os.environ.get("HOUSE"),
            "parliament": os.environ.get("PARLIAMENT"),
            "session": os.environ.get("SESSION"),
            "reason": reason,
            "skipped_at": datetime.now(timezone.utc).isoformat(),
            "scope_status": status,
        }
        Path(f"/tmp/session-skipped-{key}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
