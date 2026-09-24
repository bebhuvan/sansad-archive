#!/usr/bin/env python3
"""Extract a cloud scope in durable batches, preserving work across runner exits."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description=__doc__)
    for name in ("house", "parliament", "session", "repo", "checkpoint-path"):
        command.add_argument(f"--{name}", required=True)
    command.add_argument("--chunk-documents", type=int, default=100)
    command.add_argument("--workers", type=int, default=2)
    command.add_argument("--reserve-seconds", type=int, default=7200)
    return command


def extract(args: argparse.Namespace) -> int:
    if args.chunk_documents < 1 or args.workers < 1 or args.reserve_seconds < 0:
        raise ValueError("chunk-documents and workers must be positive; reserve-seconds nonnegative")
    deadline = int(os.environ.get("JOB_DEADLINE", "0"))
    checkpoint = [
        sys.executable, "scripts/cloud_state.py", "save", "--repo", args.repo,
        "--path-in-repo", args.checkpoint_path,
    ]
    chunks = 0
    while True:
        if deadline and time.time() >= deadline - args.reserve_seconds:
            print(f"Extraction work deadline reached after {chunks} chunks; resuming next run", flush=True)
            return 0
        with tempfile.TemporaryDirectory() as temp_dir:
            summary_path = Path(temp_dir) / "summary.json"
            command = [
                sys.executable, "-m", "sansad_pipeline", "process-scope",
                "--house", args.house, "--parliament", args.parliament,
                "--session", args.session, "--workers", str(args.workers),
                "--limit", str(args.chunk_documents), "--pending-only",
                "--summary-out", str(summary_path),
            ]
            result = subprocess.run(command, check=False)
            if not summary_path.is_file():
                print("Extraction command did not write its summary", file=sys.stderr)
                return 1
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        selected = summary["selected"]
        if not selected:
            print(f"Extraction complete after {chunks} checkpointed chunks", flush=True)
            return 0
        # Even when one PDF fails, retain all other completed PDFs before failing the job.
        subprocess.run(checkpoint, check=True)
        chunks += 1
        print(f"Extraction chunk {chunks}: {summary}; checkpoint saved", flush=True)
        if result.returncode or summary["failures"]:
            return 1


if __name__ == "__main__":
    raise SystemExit(extract(parser().parse_args()))
