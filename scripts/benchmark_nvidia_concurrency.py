#!/usr/bin/env python3
"""Empirically find a stable NVIDIA NIM concurrency for real Sansad pages."""
from __future__ import annotations

import argparse
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sansad_pipeline.cli import DEFAULT_CONFIG
from sansad_pipeline.config import load_config
from sansad_pipeline.nvidia import NvidiaAdjudicator
from sansad_pipeline.storage import Store


def candidates(store: Store, model: str, count: int) -> list[dict]:
    rows = store.db.all(
        """WITH scoped AS (
               SELECT DISTINCT c.document_sha256
                 FROM census_records c
                WHERE (c.house='lok_sabha' AND c.parliament_number='18' AND c.session='8')
                   OR (c.house='rajya_sabha' AND COALESCE(c.parliament_number,'')=''
                                               AND c.session='271')
           ), latest AS (
               SELECT s.document_sha256,
                      (SELECT MAX(r.id) FROM runs r
                        WHERE r.document_sha256=s.document_sha256
                          AND r.status='complete') run_id
                 FROM scoped s
           )
           SELECT l.document_sha256,p.run_id,p.page_number,p.text_chars,p.route,
                  p.validation_status
             FROM latest l JOIN pages p ON p.run_id=l.run_id
            WHERE p.text_chars BETWEEN 200 AND 6000
              AND NOT EXISTS (
                  SELECT 1 FROM adjudications a
                   WHERE a.run_id=p.run_id AND a.page_number=p.page_number
                     AND a.provider='nvidia' AND a.model=?
              )
            ORDER BY CASE WHEN p.validation_status='review' THEN 0 ELSE 1 END,
                     p.text_chars,p.run_id,p.page_number
            LIMIT ?""",
        (model, count),
    )
    return [dict(row) for row in rows]


def attempt_events(store: Store, item: dict, model: str) -> list[dict]:
    row = store.db.one(
        """SELECT response_path FROM adjudications
            WHERE run_id=? AND page_number=? AND provider='nvidia' AND model=?
            ORDER BY id DESC LIMIT 1""",
        (item["run_id"], item["page_number"], model),
    )
    if not row:
        return []
    path = Path(row["response_path"]).with_name("request-attempts.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []


def run_level(config, store: Store, items: list[dict], concurrency: int) -> dict:
    barrier = threading.Barrier(len(items))

    def one(item: dict) -> dict:
        reviewer = NvidiaAdjudicator(config)
        barrier.wait()
        started = time.monotonic()
        result = reviewer.adjudicate(
            item["document_sha256"], pages=[int(item["page_number"])], all_pages=True
        )
        elapsed = time.monotonic() - started
        events = attempt_events(store, item, config.nvidia.model)
        statuses = [event.get("http_status") for event in events if event.get("http_status")]
        return {
            **item,
            "elapsed_seconds": round(elapsed, 3),
            "success": bool(result["completed"]),
            "failed": result["failed"],
            "http_statuses": statuses,
            "attempts": len(events),
        }

    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(one, item) for item in items]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.monotonic() - started
    latencies = [item["elapsed_seconds"] for item in results]
    statuses = [status for item in results for status in item["http_statuses"]]
    successes = sum(item["success"] for item in results)
    return {
        "concurrency": concurrency,
        "requests": len(results),
        "successes": successes,
        "failures": len(results) - successes,
        "wall_seconds": round(elapsed, 3),
        "throughput_pages_per_minute": round(60 * successes / elapsed, 3),
        "median_latency_seconds": round(statistics.median(latencies), 3),
        "max_latency_seconds": round(max(latencies), 3),
        "http_status_counts": {str(code): statuses.count(code) for code in sorted(set(statuses))},
        "results": sorted(results, key=lambda item: (item["run_id"], item["page_number"])),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--levels", default="1,2,4,6,8")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",")]
    if any(value < 1 for value in levels):
        raise SystemExit("concurrency levels must be positive")
    config = load_config(DEFAULT_CONFIG)
    store = Store(config)
    store.initialize()
    pool = candidates(store, config.nvidia.model, sum(levels))
    if len(pool) < sum(levels):
        raise SystemExit(f"need {sum(levels)} untranscribed pages; found {len(pool)}")
    report = {
        "model": config.nvidia.model,
        "method": "one simultaneous wave per concurrency level; real pages with 200-6000 local characters",
        "levels": [],
    }
    offset = 0
    for level in levels:
        result = run_level(config, store, pool[offset:offset + level], level)
        report["levels"].append(result)
        offset += level
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "results"}), flush=True)
        if result["failures"] or result["http_status_counts"].get("429"):
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
