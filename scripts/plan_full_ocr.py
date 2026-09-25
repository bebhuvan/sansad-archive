#!/usr/bin/env python3
"""Plan full-page OCR only for published, checkpointed scopes lacking coverage."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sansad_pipeline.config import load_config  # noqa: E402
from scripts.full_ocr_layer import completion_evidence  # noqa: E402


MARKER = re.compile(
    r"^state/(snapshot-complete|complete)/(snapshot-complete|session-complete)-"
    r"(lok_sabha|rajya_sabha)-p([A-Za-z0-9_-]+)-s([A-Za-z0-9_-]+)\.json$"
)


def plan(repo: str, token: str, *, house: str, max_scopes: int,
         api: HfApi | None = None) -> list[dict[str, str]]:
    api = api or HfApi(token=token)
    files = api.list_repo_files(repo, repo_type="dataset")
    version = load_config(Path("pipeline.toml")).liteparse.version
    candidates: list[dict[str, str]] = []
    for path in sorted(files):
        match = MARKER.fullmatch(path)
        if not match or match.group(3) != house:
            continue
        source = "elibrary" if match.group(1) == "snapshot-complete" else "current"
        if match.group(2) != ("snapshot-complete" if source == "elibrary" else "session-complete"):
            raise RuntimeError(f"inconsistent completion marker path: {path}")
        _, _, selected_house, parliament, session = match.groups()
        evidence = completion_evidence(repo, source, selected_house,
                                       parliament, session, token, api)
        key = f"{selected_house}-p{parliament}-s{session}"
        prefix = f"layers/full-ocr/{key}/"
        complete_paths = [item for item in files if item.startswith(prefix)
                          and item.endswith("/complete.json")]
        done = False
        for complete_path in complete_paths:
            manifest = json.loads(Path(hf_hub_download(
                repo, complete_path, repo_type="dataset", token=token)).read_text())
            if (manifest.get("completion_marker") == evidence
                    and manifest.get("engine") == "liteparse"
                    and manifest.get("engine_version") == version
                    and manifest.get("source") == source
                    and manifest.get("scope") == key
                    and manifest.get("pages") ==
                    (json.loads(Path(hf_hub_download(
                        repo, path, repo_type="dataset", token=token)).read_text())
                     .get("scope_status") or {}).get("pages")):
                done = True
                break
        if not done:
            candidates.append({"source": source, "house": selected_house,
                               "parliament": parliament, "session": session})
    return candidates[:max_scopes]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--house", default="lok_sabha")
    parser.add_argument("--max-scopes", type=int, default=2)
    args = parser.parse_args()
    if args.max_scopes < 1:
        parser.error("max-scopes must be positive")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")
    matrix = {"include": plan(args.repo, token, house=args.house,
                              max_scopes=args.max_scopes)}
    print(json.dumps(matrix, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
