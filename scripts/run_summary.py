#!/usr/bin/env python3
"""Write the cloud run summary JSON from workflow environment variables."""
from __future__ import annotations

import json
import os
from pathlib import Path


def is_session_complete(status: dict, *, tranche_path: str, all_pages: bool,
                        unlimited: bool, adjudication_required: bool) -> bool:
    acquisition = status.get("acquisition") or {}
    unsupported_html = int(status.get("unsupported_html_records") or 0)
    pages = status.get("pages", 0)
    return bool(
        status.get("census_status") == "complete"
        and status.get("records")
        and acquisition.get("discovered", 0) == 0
        and acquisition.get("failed", 0) == unsupported_html
        and acquisition.get("downloaded", 0) + unsupported_html == status.get("records")
        and status.get("acquired_documents", 0) > 0
        and status.get("processed_documents") == status.get("acquired_documents")
        and pages > 0
        and status.get("openrouter_adjudicated_pages", 0) == pages
        and adjudication_required and all_pages and unlimited
        and bool(tranche_path)
    )


def main() -> int:
    summary = {
        "workflow_run": (
            f"{os.environ.get('GITHUB_SERVER_URL', '')}/"
            f"{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/"
            f"{os.environ.get('GITHUB_RUN_ID', '')}"
        ).strip("/"),
        "commit": os.environ.get("GITHUB_SHA"),
        "inputs": {
            key.lower(): os.environ.get(key)
            for key in ("SOURCE", "HOUSE", "PARLIAMENT", "SESSION", "LIMIT", "MAX_PAGES",
                        "ALL_PAGES", "INCLUDE_OCR", "VERIFY_SAMPLE")
        },
        "tranche": os.environ.get("TRANCHE", ""),
        "tranche_path": os.environ.get("PATH_IN_REPO", ""),
    }
    status_path = Path("/tmp/scope-status.json")
    if status_path.is_file():
        try:
            summary["scope_status"] = json.loads(status_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            summary["scope_status_error"] = "unreadable scope-status.json"
    status = summary.get("scope_status") or {}
    snapshot_path = Path("/tmp/census-snapshot-manifest.json")
    if os.environ.get("SOURCE") == "elibrary" and snapshot_path.is_file():
        summary["census_snapshot"] = json.loads(snapshot_path.read_text(encoding="utf-8"))
    adjudication_required = os.environ.get("MAX_PAGES", "0") != "0"
    adjudication_done = bool(
        status.get("pages", 0) > 0
        and status.get("openrouter_adjudicated_pages", 0) == status.get("pages", 0)
    )
    summary["adjudication_required"] = adjudication_required
    summary["adjudication_complete"] = adjudication_done
    summary["session_complete"] = is_session_complete(
        status, tranche_path=summary["tranche_path"],
        all_pages=os.environ.get("ALL_PAGES") == "true",
        unlimited=(os.environ.get("LIMIT") == "0" and os.environ.get("SOURCE", "current") == "current"),
        adjudication_required=adjudication_required,
    )
    summary["snapshot_complete"] = bool(
        os.environ.get("SOURCE") == "elibrary"
        and summary.get("census_snapshot", {}).get("sha256")
        and is_session_complete(
            status, tranche_path=summary["tranche_path"],
            all_pages=os.environ.get("ALL_PAGES") == "true",
            unlimited=os.environ.get("LIMIT") == "0",
            adjudication_required=adjudication_required,
        )
    )
    Path("/tmp/run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if summary["session_complete"]:
        key = (
            f"{os.environ.get('HOUSE', '')}-p{os.environ.get('PARLIAMENT', '')}"
            f"-s{os.environ.get('SESSION', '')}"
        )
        Path(f"/tmp/session-complete-{key}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if summary["snapshot_complete"]:
        key = (
            f"{os.environ.get('HOUSE', '')}-p{os.environ.get('PARLIAMENT', '')}"
            f"-s{os.environ.get('SESSION', '')}"
        )
        Path(f"/tmp/snapshot-complete-{key}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
