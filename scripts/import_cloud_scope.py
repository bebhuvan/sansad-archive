#!/usr/bin/env python3
"""Restore a complete dated eLibrary scope census from a verified HF snapshot."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from huggingface_hub import hf_hub_download  # noqa: E402

from sansad_pipeline.census import Census  # noqa: E402
from sansad_pipeline.config import load_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--house", required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--snapshot-root", default="state/census/snapshot-2026-08-01")
    args = parser.parse_args()
    if args.house != "lok_sabha":
        parser.error("the eLibrary snapshot currently covers Lok Sabha only")

    token = os.environ.get("HF_TOKEN") or None
    manifest_path = hf_hub_download(
        args.repo, args.snapshot_root + ".json", repo_type="dataset", token=token,
    )
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    expected_path = args.snapshot_root + ".jsonl.gz"
    if manifest.get("path") != expected_path or not manifest.get("sha256"):
        raise RuntimeError("HF census manifest lacks the expected archive path or SHA-256")
    archive = Path(hf_hub_download(
        args.repo, expected_path, repo_type="dataset", token=token,
    ))
    if archive.stat().st_size != manifest.get("bytes"):
        raise RuntimeError("HF census archive size differs from its manifest")
    config = load_config(Path(__file__).resolve().parents[1] / "pipeline.toml")
    result = Census(config).import_snapshot(
        archive, source="elibrary", house=args.house,
        parliament=args.parliament, session=args.session,
        expected_sha256=manifest["sha256"],
    )
    print(json.dumps({"snapshot_date": manifest.get("snapshot_date"), **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
