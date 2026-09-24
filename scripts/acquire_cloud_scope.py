#!/usr/bin/env python3
"""Acquire one scope in durable, time-bounded chunks on an Actions runner."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cloud_state import save  # noqa: E402
from sansad_pipeline.census import Census  # noqa: E402
from sansad_pipeline.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--house", required=True)
    parser.add_argument("--parliament", default="")
    parser.add_argument("--session", required=True)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--checkpoint-path", required=True)
    parser.add_argument("--source", choices=("current", "elibrary"), default="current")
    parser.add_argument("--chunk", type=int, default=1000)
    parser.add_argument("--budget-seconds", type=int, default=7200)
    args = parser.parse_args()
    if args.limit < 0 or args.chunk < 1 or args.budget_seconds < 1:
        parser.error("limit must be non-negative; chunk and budget must be positive")

    census = Census(load_config(Path(__file__).resolve().parents[1] / "pipeline.toml"))
    source_filter = (
        "AND record_id LIKE 'elibrary_%'" if args.source == "elibrary"
        else "AND record_id NOT LIKE 'elibrary_%'"
    )
    started = time.monotonic()
    selected = downloaded = failed = 0
    stopped_low_disk = False
    already_acquired = census.store.db.one(
        f"""SELECT COUNT(*) n FROM census_records
           WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
             AND document_sha256 IS NOT NULL {source_filter}""",
        (args.house, args.parliament, args.session),
    )["n"]
    while time.monotonic() - started < args.budget_seconds:
        remaining = args.limit - already_acquired - selected if args.limit else args.chunk
        if remaining <= 0:
            break
        chunk = min(args.chunk, remaining)
        result = census.acquire_questions(
            source=args.source, house=args.house,
            lok_sabha=args.parliament or None, session=args.session,
            limit=chunk, workers=4, min_free_gib=2,
        )
        selected += result["selected"]
        downloaded += result["downloaded"]
        failed += result["failed"]
        stopped_low_disk = result["stopped_low_disk"]
        if result["downloaded"]:
            save(args.repo, args.checkpoint_path, token=os.environ.get("HF_TOKEN"))
        if result["selected"] < chunk or stopped_low_disk:
            break

    pending_failures = census.store.db.one(
        f"""SELECT COUNT(*) n FROM census_records
           WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
             AND acquisition_status='failed' {source_filter}""",
        (args.house, args.parliament, args.session),
    )["n"]
    if pending_failures and not stopped_low_disk and time.monotonic() - started < args.budget_seconds:
        retry = census.acquire_questions(
            source=args.source, house=args.house,
            lok_sabha=args.parliament or None, session=args.session,
            retry_failed=True, workers=4, min_free_gib=2,
        )
        downloaded += retry["downloaded"]
        stopped_low_disk = retry["stopped_low_disk"]
        if retry["downloaded"]:
            save(args.repo, args.checkpoint_path, token=os.environ.get("HF_TOKEN"))

    remaining = census.store.db.one(
        f"""SELECT
             SUM(CASE WHEN acquisition_status='discovered' THEN 1 ELSE 0 END) discovered,
             SUM(CASE WHEN acquisition_status='failed' THEN 1 ELSE 0 END) failed
           FROM census_records
           WHERE house=? AND COALESCE(parliament_number,'')=? AND session=? {source_filter}""",
        (args.house, args.parliament, args.session),
    )
    print(json.dumps({
        "selected": selected, "downloaded": downloaded,
        "failed": int(remaining["failed"] or 0),
        "discovered": int(remaining["discovered"] or 0),
        "stopped_low_disk": stopped_low_disk,
        "budget_reached": time.monotonic() - started >= args.budget_seconds,
    }, indent=2))
    return 1 if stopped_low_disk else 0


if __name__ == "__main__":
    raise SystemExit(main())
