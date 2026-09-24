#!/usr/bin/env python3
"""Append a verified new-accession prefix to a dated eLibrary census snapshot.

This is an incremental inventory, not a full recrawl or live-completeness
claim. It fails closed if the live count, accession order, ID boundary, or
remote artifact checks do not support the append.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
import tempfile
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sansad_pipeline.sources.elibrary import (  # noqa: E402
    records_from_search_response, search_page,
)
from scripts.cloud_state import _commit_with_retry  # noqa: E402


ACCESSION_SORT = "dc.date.accessioned,desc"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_base(archive: Path, manifest: dict) -> set[str]:
    if archive.stat().st_size != manifest["bytes"] or sha256_file(archive) != manifest["sha256"]:
        raise RuntimeError("base census snapshot byte size or SHA-256 mismatch")
    ids: set[str] = set()
    elibrary_count = lines = 0
    with gzip.open(archive, "rt", encoding="utf-8") as stream:
        for lines, line in enumerate(stream, 1):
            record_id = str(json.loads(line)["record_id"])
            if not record_id or record_id in ids:
                raise RuntimeError(f"base census snapshot has duplicate/empty ID at line {lines}")
            ids.add(record_id)
            elibrary_count += record_id.startswith("elibrary_ls_question_")
    if (lines != manifest["record_count"]
            or elibrary_count != manifest["sources"]["elibrary_lok_sabha"]):
        raise RuntimeError("base census snapshot counts do not match its manifest")
    return ids


def accession_values(response: dict, page: int) -> list[str]:
    hits = response["_embedded"]["searchResult"]["_embedded"]["objects"]
    values = []
    for hit in hits:
        metadata = hit["_embedded"]["indexableObject"].get("metadata") or {}
        entries = metadata.get("dc.date.accessioned") or []
        value = str(entries[0].get("value") or "") if entries else ""
        if not value:
            raise RuntimeError(f"sorted eLibrary page {page} lacks accession timestamp")
        values.append(value)
    if values != sorted(values, reverse=True):
        raise RuntimeError(f"sorted eLibrary page {page} is not accession-descending")
    return values


def collect_prefix(existing: set[str], old_elibrary_count: int, *, page_size: int = 100,
                   overlap_pages: int = 10, max_new: int = 10000,
                   fetch=search_page) -> tuple[list[dict], dict]:
    first = fetch(page=0, page_size=page_size, sort=ACCESSION_SORT)
    live_count = int(first["_embedded"]["searchResult"]["page"]["totalElements"])
    expected_new = live_count - old_elibrary_count
    if expected_new < 0 or expected_new > max_new:
        raise RuntimeError(f"live/base count delta {expected_new} outside 0..{max_new}")
    # A stable total is not proof of a stable inventory: one new accession
    # and one deletion can cancel out. Check the known-ID overlap even then.
    pages_needed = min(
        math.ceil(live_count / page_size),
        math.ceil(expected_new / page_size) + overlap_pages,
    )
    required_overlap = min(overlap_pages * page_size, old_elibrary_count)
    new: list[dict] = []
    seen: set[str] = set()
    previous_last: str | None = None
    known_checked = 0
    first_ids: list[str] = []
    for page in range(pages_needed):
        response = first if page == 0 else fetch(page=page, page_size=page_size,
                                                 sort=ACCESSION_SORT)
        reported = int(response["_embedded"]["searchResult"]["page"]["totalElements"])
        if reported != live_count:
            raise RuntimeError("eLibrary live count changed during incremental census")
        dates = accession_values(response, page)
        if previous_last is not None and dates and previous_last < dates[0]:
            raise RuntimeError(f"accession sort reversed across page {page}")
        if dates:
            previous_last = dates[-1]
        records = records_from_search_response(response, page=page)
        if page == 0:
            first_ids = [record.record_id for record in records[:5]]
        for offset, record in enumerate(records):
            position = page * page_size + offset
            if record.record_id in seen:
                raise RuntimeError(f"duplicate ID across sorted pages: {record.record_id}")
            seen.add(record.record_id)
            if position < expected_new:
                if record.record_id in existing:
                    raise RuntimeError(f"known ID inside new-accession prefix at {position}")
                accession = dates[offset]
                updated = replace(
                    record,
                    api_params={**record.api_params, "sort": ACCESSION_SORT},
                    raw={**record.raw, "dc.date.accessioned": [accession]},
                )
                new.append({**asdict(updated), "discovered_at": datetime.now(timezone.utc).isoformat(),
                            "acquisition_status": "discovered", "document_sha256": None,
                            "last_error": None})
            else:
                if record.record_id not in existing:
                    raise RuntimeError(f"unknown ID beyond count-delta boundary at {position}")
                known_checked += 1
        print(f"accession page={page} new={len(new)} known_overlap={known_checked}", flush=True)
        time.sleep(0.2)
    last = fetch(page=0, page_size=page_size, sort=ACCESSION_SORT)
    if (int(last["_embedded"]["searchResult"]["page"]["totalElements"]) != live_count
            or [r.record_id for r in records_from_search_response(last, page=0)[:5]] != first_ids):
        raise RuntimeError("eLibrary accession head changed during incremental census")
    if len(new) != expected_new or known_checked < required_overlap:
        raise RuntimeError("incremental census did not prove its new/known boundary")
    return new, {"live_count": live_count, "expected_new": expected_new,
                 "overlap_checked": known_checked, "sort": ACCESSION_SORT,
                 "first_ids": first_ids}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base-root", default="state/census/snapshot-2026-08-01")
    parser.add_argument("--output-root")
    parser.add_argument("--overlap-pages", type=int, default=10)
    parser.add_argument("--max-new", type=int, default=10000)
    args = parser.parse_args()
    if args.overlap_pages < 1 or args.max_new < 1:
        parser.error("overlap-pages and max-new must be positive")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required for snapshot upload")
    base_manifest = json.loads(Path(hf_hub_download(
        args.repo, args.base_root + ".json", repo_type="dataset", token=token,
    )).read_text(encoding="utf-8"))
    if base_manifest.get("path") != args.base_root + ".jsonl.gz":
        raise RuntimeError("base census manifest path mismatch")
    base_archive = Path(hf_hub_download(
        args.repo, base_manifest["path"], repo_type="dataset", token=token,
    ))
    existing = load_base(base_archive, base_manifest)
    new, evidence = collect_prefix(
        existing, base_manifest["sources"]["elibrary_lok_sabha"],
        overlap_pages=args.overlap_pages, max_new=args.max_new,
    )
    if not new:
        print(json.dumps({"unchanged": True, **evidence}))
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    root = args.output_root or f"state/census/snapshot-{stamp}"
    archive_remote = root + ".jsonl.gz"
    manifest_remote = root + ".json"
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / "snapshot.jsonl.gz"
        manifest_path = Path(temporary) / "manifest.json"
        with gzip.open(base_archive, "rt", encoding="utf-8") as source, gzip.open(
            archive, "wt", encoding="utf-8", compresslevel=6,
        ) as target:
            for line in source:
                target.write(line)
            for record in new:
                target.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        manifest = {
            "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
            "snapshot_timestamp": datetime.now(timezone.utc).isoformat(),
            "record_count": base_manifest["record_count"] + len(new),
            "sha256": sha256_file(archive), "bytes": archive.stat().st_size,
            "path": archive_remote,
            "sources": {**base_manifest["sources"],
                        "elibrary_lok_sabha": evidence["live_count"]},
            "base_snapshot": {"path": base_manifest["path"],
                              "sha256": base_manifest["sha256"]},
            "incremental_evidence": evidence,
            "limitations": "Accession-prefix append with overlap, not a full recrawl. Existing metadata remains dated; changes/deletions deeper in the collection are not detected. Not evidence that PDFs are acquired.",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        api = HfApi(token=token)
        _commit_with_retry(api, repo=args.repo, operations=[
            CommitOperationAdd(path_in_repo=archive_remote, path_or_fileobj=str(archive)),
            CommitOperationAdd(path_in_repo=manifest_remote, path_or_fileobj=str(manifest_path)),
        ], message=f"Incremental eLibrary census {stamp}")
        remote = api.get_paths_info(args.repo, [archive_remote], repo_type="dataset", expand=True)
        if (len(remote) != 1 or remote[0].size != manifest["bytes"]
                or not remote[0].lfs or remote[0].lfs.sha256 != manifest["sha256"]):
            raise RuntimeError("uploaded eLibrary archive failed remote LFS verification")
        saved_manifest = json.loads(Path(hf_hub_download(
            args.repo, manifest_remote, repo_type="dataset", token=token,
            force_download=True,
        )).read_text(encoding="utf-8"))
        if saved_manifest != manifest:
            raise RuntimeError("uploaded eLibrary manifest differs from local artifact")
        print(json.dumps({"snapshot_root": root, **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
