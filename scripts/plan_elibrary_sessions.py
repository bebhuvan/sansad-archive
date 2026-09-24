#!/usr/bin/env python3
"""Plan dated eLibrary Lok Sabha scopes without claiming live-source completeness."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.run_summary import is_session_complete  # noqa: E402
from sansad_pipeline.sources.elibrary import normalize_session_label  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def scope_counts(archive: Path, expected_sha256: str) -> Counter[tuple[str, str]]:
    if sha256_file(archive) != expected_sha256:
        raise ValueError("eLibrary census snapshot SHA-256 mismatch")
    counts: Counter[tuple[str, str]] = Counter()
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            record = json.loads(line)
            if record.get("record_id", "").startswith("elibrary_") and record.get("house") == "lok_sabha":
                parliament = record.get("parliament_number")
                session, _ = normalize_session_label(
                    str(record.get("session") or ""), record.get("members") or []
                )
                if not parliament or not session:
                    raise ValueError(f"eLibrary record {line_number} lacks its session scope")
                counts[(str(parliament), str(session))] += 1
    return counts


def roman_value(value: str) -> int:
    if value.isdecimal():
        return int(value)
    if not re.fullmatch(r"[IVXLCDM]+", value.upper()):
        raise ValueError(f"unexpected eLibrary session label: {value}")
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total = previous = 0
    for character in value.upper()[::-1]:
        current = values.get(character)
        if current is None:
            raise ValueError(f"unexpected eLibrary session label: {value}")
        total += -current if current < previous else current
        previous = current
    return total


def completed_scopes(repo: str, snapshot_sha256: str) -> set[tuple[str, str]]:
    prefix = "state/snapshot-complete/snapshot-complete-lok_sabha-p"
    files = HfApi().list_repo_files(repo, repo_type="dataset")
    done = set()
    for path in files:
        if not path.startswith(prefix) or not path.endswith(".json"):
            continue
        payload = json.loads(Path(hf_hub_download(repo, path, repo_type="dataset")).read_text())
        inputs = payload.get("inputs") or {}
        scope = (str(inputs.get("parliament") or ""), str(inputs.get("session") or ""))
        if not path.endswith(f"-p{scope[0]}-s{scope[1]}.json"):
            continue
        if (payload.get("snapshot_complete") is True
                and payload.get("session_complete") is False
                and inputs.get("source") == "elibrary"
                and (payload.get("census_snapshot") or {}).get("sha256") == snapshot_sha256
                and is_session_complete(
                    payload.get("scope_status") or {},
                    tranche_path=str(payload.get("tranche_path") or ""),
                    all_pages=str(inputs.get("all_pages") or "").lower() == "true",
                    unlimited=str(inputs.get("limit") or "") == "0",
                    adjudication_required=str(inputs.get("max_pages") or "0") != "0",
                )):
            done.add(scope)
    return done


def main() -> int:
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument("--repo", required=True)
    command.add_argument("--max", type=int, default=2)
    command.add_argument("--snapshot-root", default="state/census/snapshot-2026-08-01")
    command.add_argument("--exclude", action="append", default=[])
    args = command.parse_args()
    if args.max < 0:
        command.error("--max must be non-negative")
    manifest = json.loads(Path(hf_hub_download(
        args.repo, args.snapshot_root + ".json", repo_type="dataset"
    )).read_text())
    archive_path = args.snapshot_root + ".jsonl.gz"
    if manifest.get("path") != archive_path or not manifest.get("sha256"):
        raise RuntimeError("invalid dated eLibrary snapshot manifest")
    archive = Path(hf_hub_download(args.repo, archive_path, repo_type="dataset"))
    if archive.stat().st_size != manifest.get("bytes"):
        raise RuntimeError("dated eLibrary snapshot size mismatch")
    counts = scope_counts(archive, manifest["sha256"])
    done = completed_scopes(args.repo, manifest["sha256"])
    excluded = {part.strip() for entry in args.exclude for part in entry.split(",") if part.strip()}
    valid_counts = {}
    anomalous = {}
    for key, count in counts.items():
        try:
            roman_value(key[1])
        except ValueError:
            anomalous[key] = count
        else:
            valid_counts[key] = count
    scopes = [
        {"house": "lok_sabha", "parliament": parliament, "session": session}
        for parliament, session in sorted(valid_counts, key=lambda key: (int(key[0]), roman_value(key[1])))
        if (parliament, session) not in done
        and f"lok_sabha:{parliament}:{session}" not in excluded
    ]
    if args.max:
        scopes = scopes[:args.max]
    print(f"dated snapshot {manifest.get('snapshot_date')}: {len(counts)} scopes, "
          f"{sum(counts.values())} records, {len(done)} proven complete", file=sys.stderr)
    if anomalous:
        print(f"quarantined malformed source session labels: {anomalous}", file=sys.stderr)
    for scope in scopes:
        key = (scope["parliament"], scope["session"])
        print(f"queue LS {key[0]}/{key[1]} snapshot_records={counts[key]}", file=sys.stderr)
    print("matrix=" + json.dumps({"include": scopes}))
    print("count=" + str(len(scopes)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
