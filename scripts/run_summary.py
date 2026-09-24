#!/usr/bin/env python3
"""Write the cloud run summary JSON from workflow environment variables."""
from __future__ import annotations

import json
import os
from pathlib import Path


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
            for key in ("HOUSE", "PARLIAMENT", "SESSION", "LIMIT", "MAX_PAGES",
                        "ALL_PAGES", "VERIFY_SAMPLE")
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
    acquisition = status.get("acquisition") or {}
    summary["session_complete"] = bool(
        status.get("records")
        and acquisition.get("discovered", 0) == 0
        and acquisition.get("failed", 0) == 0
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
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
