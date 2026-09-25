#!/usr/bin/env python3
"""Resumable two-pass census of the official Lok Sabha eLibrary collection.

An ascending first pass writes immutable HF record shards. An ascending audit
pass must reproduce each normalized page hash before a dated snapshot is
published. This detects offset shifts and metadata changes in the scanned
prefix; DSpace itself does not offer transactional snapshot isolation.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sansad_pipeline.sources.elibrary import records_from_search_response, search_page  # noqa: E402
from scripts.cloud_state import _commit_with_retry  # noqa: E402
from scripts.refresh_elibrary_census import sha256_file  # noqa: E402


SORT = "dc.date.accessioned,asc"
PAGE_SIZE = 100
SHARD_PAGES = 100
SHARD_RE = re.compile(r"/(first|audit)/part-(\d{6})-(\d{6})\.json$")


def normalized_page(response: dict, page: int, target_count: int) -> tuple[list[str], str, str]:
    result = response["_embedded"]["searchResult"]
    if int(result["page"]["totalElements"]) < target_count:
        raise RuntimeError("eLibrary live count fell below the scan's fixed target")
    hits = result["_embedded"]["objects"]
    records = records_from_search_response(response, page=page)
    take = min(PAGE_SIZE, target_count - page * PAGE_SIZE)
    if take < 1 or len(records) < take or len(hits) < take:
        raise RuntimeError(f"eLibrary page {page} no longer covers the fixed prefix")
    values: list[str] = []
    lines: list[str] = []
    for hit, record in zip(hits[:take], records[:take]):
        metadata = hit["_embedded"]["indexableObject"].get("metadata") or {}
        entries = metadata.get("dc.date.accessioned") or []
        accession = str(entries[0].get("value") or "") if entries else ""
        if not accession:
            raise RuntimeError(f"eLibrary page {page} lacks an accession timestamp")
        values.append(accession)
        updated = replace(record, api_params={**record.api_params, "sort": SORT},
                          raw={**record.raw, "dc.date.accessioned": [accession]})
        lines.append(json.dumps(asdict(updated), ensure_ascii=False,
                                sort_keys=True, separators=(",", ":")) + "\n")
    if values != sorted(values):
        raise RuntimeError(f"eLibrary page {page} is not accession-ascending")
    return lines, values[0], values[-1]


def page_digest(lines: list[str]) -> str:
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def shard_paths(root: str, kind: str, start: int, end: int) -> tuple[str, str]:
    stem = f"{root}/{kind}/part-{start:06d}-{end:06d}"
    return stem + ".jsonl.gz", stem + ".json"


def load_shards(repo: str, root: str, kind: str, *, token: str,
                api: HfApi) -> list[dict]:
    prefix = f"{root}/{kind}/"
    files = api.list_repo_files(repo, repo_type="dataset")
    manifests = sorted(path for path in files if path.startswith(prefix)
                       and path.endswith(".json"))
    shards = []
    expected_start = 0
    for path in manifests:
        match = SHARD_RE.search(path)
        if not match or match.group(1) != kind:
            raise RuntimeError(f"unexpected census shard manifest {path}")
        start, end = map(int, match.group(2, 3))
        if (start != expected_start or end < start
                or end - start + 1 > SHARD_PAGES):
            raise RuntimeError(f"noncontiguous or oversized {kind} census shard at {path}")
        item = json.loads(Path(hf_hub_download(
            repo, path, repo_type="dataset", token=token)).read_text())
        if (item.get("kind") != kind or item.get("start_page") != start
                or item.get("end_page") != end or item.get("sort") != SORT
                or len(item.get("page_hashes") or []) != end - start + 1):
            raise RuntimeError(f"invalid {kind} census shard manifest {path}")
        shards.append(item)
        expected_start = end + 1
    return shards


def verify_first_shard_files(repo: str, root: str, shards: list[dict], *,
                             token: str, api: HfApi) -> None:
    paths = [shard_paths(root, "first", row["start_page"], row["end_page"])[0]
             for row in shards]
    if not paths:
        return
    remote = {item.path: item for item in api.get_paths_info(
        repo, paths, repo_type="dataset", expand=True)}
    for row, path in zip(shards, paths):
        info = remote.get(path)
        if info is None or info.size != row.get("bytes"):
            raise RuntimeError(f"HF census shard failed remote LFS verification: {path}")
        if info.lfs:
            if info.lfs.sha256 != row.get("sha256"):
                raise RuntimeError(f"HF census shard LFS hash mismatch: {path}")
        else:
            local = Path(hf_hub_download(repo, path, repo_type="dataset", token=token))
            if sha256_file(local) != row.get("sha256"):
                raise RuntimeError(f"HF census shard Git-file hash mismatch: {path}")


def verified_shard_lines(path: Path, shard: dict) -> Iterator[str]:
    """Check decompressed row counts and page hashes before using a shard."""
    count = 0
    with gzip.open(path, "rt", encoding="utf-8") as source:
        for page in range(shard["start_page"], shard["end_page"] + 1):
            expected = min(PAGE_SIZE, shard["target_count"] - page * PAGE_SIZE)
            lines = [source.readline() for _ in range(expected)]
            if any(not line for line in lines):
                raise RuntimeError(f"census shard is truncated at page {page}: {path}")
            if page_digest(lines) != shard["page_hashes"][page - shard["start_page"]]:
                raise RuntimeError(f"census shard page hash mismatch at page {page}: {path}")
            count += len(lines)
            yield from lines
        if source.readline():
            raise RuntimeError(f"census shard has extra records: {path}")
    if count != shard["record_count"]:
        raise RuntimeError(f"census shard row count mismatch: {path}")


def scan_shard(fetch, *, kind: str, start: int, end: int, target_count: int,
               prior_accession: str | None, first: dict | None,
               sleep_seconds: float) -> tuple[list[str], dict]:
    all_lines: list[str] = []
    hashes: list[str] = []
    first_accession = last_accession = None
    for page in range(start, end + 1):
        response = fetch(page=page, page_size=PAGE_SIZE, sort=SORT)
        lines, page_first, page_last = normalized_page(response, page, target_count)
        if prior_accession is not None and page_first < prior_accession:
            raise RuntimeError(f"eLibrary accession order reversed at page {page}")
        prior_accession = page_last
        first_accession = first_accession or page_first
        last_accession = page_last
        hashes.append(page_digest(lines))
        if kind == "first":
            all_lines.extend(lines)
        else:
            assert first is not None
            if hashes[-1] != first["page_hashes"][page - start]:
                raise RuntimeError(f"eLibrary record set or metadata changed at page {page}")
        time.sleep(sleep_seconds)
    return all_lines, {
        "kind": kind, "sort": SORT, "page_size": PAGE_SIZE,
        "target_count": target_count, "start_page": start, "end_page": end,
        "record_count": sum(min(PAGE_SIZE, target_count - page * PAGE_SIZE)
                            for page in range(start, end + 1)),
        "first_accession": first_accession, "last_accession": last_accession,
        "page_hashes": hashes,
    }


def publish_shard(api: HfApi, repo: str, root: str, kind: str,
                  lines: list[str], manifest: dict, *, token: str) -> dict:
    start, end = manifest["start_page"], manifest["end_page"]
    archive_path, manifest_path = shard_paths(root, kind, start, end)
    with tempfile.TemporaryDirectory() as directory:
        file_path = Path(directory) / "part.jsonl.gz"
        json_path = Path(directory) / "part.json"
        operations = []
        if kind == "first":
            with gzip.open(file_path, "wt", encoding="utf-8", compresslevel=6) as stream:
                stream.writelines(lines)
            manifest = {**manifest, "bytes": file_path.stat().st_size,
                        "sha256": sha256_file(file_path), "path": archive_path}
            operations.append(CommitOperationAdd(path_in_repo=archive_path,
                                                 path_or_fileobj=str(file_path)))
        json_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
        operations.append(CommitOperationAdd(path_in_repo=manifest_path,
                                             path_or_fileobj=str(json_path)))
        _commit_with_retry(api, repo=repo, operations=operations,
                           message=f"eLibrary census {kind} pages {start}-{end}")
        if kind == "first":
            verify_first_shard_files(repo, root, [manifest], token=token, api=api)
        saved = json.loads(Path(hf_hub_download(
            repo, manifest_path, repo_type="dataset", token=token,
            force_download=True)).read_text())
        if saved != manifest:
            raise RuntimeError(f"HF census shard manifest mismatch: {manifest_path}")
        return manifest


def assemble_snapshot(api: HfApi, repo: str, root: str, base_root: str,
                      first: list[dict], audit: list[dict], *, token: str) -> dict:
    if not first:
        raise RuntimeError("first census pass is missing")
    target_count = first[0]["target_count"]
    expected_pages = math.ceil(target_count / PAGE_SIZE)
    if (not audit or first[-1]["end_page"] + 1 != expected_pages
            or audit[-1]["end_page"] + 1 != expected_pages
            or [(s["start_page"], s["end_page"]) for s in first]
            != [(s["start_page"], s["end_page"]) for s in audit]):
        raise RuntimeError("both full census passes are required before publication")
    for left, right in zip(first, audit):
        if left["page_hashes"] != right["page_hashes"]:
            raise RuntimeError("census audit hashes differ from first pass")
    verify_first_shard_files(repo, root, first, token=token, api=api)
    base_manifest = json.loads(Path(hf_hub_download(
        repo, base_root + ".json", repo_type="dataset", token=token)).read_text())
    if base_manifest.get("path") != base_root + ".jsonl.gz":
        raise RuntimeError("base census manifest path mismatch")
    base_archive = Path(hf_hub_download(repo, base_manifest["path"],
                                        repo_type="dataset", token=token))
    if (base_archive.stat().st_size != base_manifest["bytes"]
            or sha256_file(base_archive) != base_manifest["sha256"]):
        raise RuntimeError("base census archive failed SHA-256 verification")
    output_root = root + "-snapshot"
    archive_remote, manifest_remote = output_root + ".jsonl.gz", output_root + ".json"
    existing = set(api.list_repo_files(repo, repo_type="dataset"))
    if archive_remote in existing or manifest_remote in existing:
        raise RuntimeError("full census output already exists; refusing to overwrite it")
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "snapshot.jsonl.gz"
        manifest_path = Path(directory) / "manifest.json"
        previous_ids: set[str] = set()
        seen: set[str] = set()
        current_count = 0
        base_count = base_elibrary_count = 0
        with gzip.open(archive, "wt", encoding="utf-8", compresslevel=6) as output:
            with gzip.open(base_archive, "rt", encoding="utf-8") as source:
                for line in source:
                    base_count += 1
                    row = json.loads(line)
                    identifier = str(row["record_id"])
                    if identifier.startswith("elibrary_ls_question_"):
                        base_elibrary_count += 1
                        if identifier in previous_ids:
                            raise RuntimeError("duplicate ID in base eLibrary census")
                        previous_ids.add(identifier)
                        continue
                    if identifier in seen:
                        raise RuntimeError("duplicate ID in base current census")
                    seen.add(identifier)
                    current_count += 1
                    output.write(line)
            if (base_count != base_manifest["record_count"]
                    or base_elibrary_count != base_manifest["sources"]["elibrary_lok_sabha"]):
                raise RuntimeError("base census counts differ from its manifest")
            for shard in first:
                path, _ = shard_paths(root, "first", shard["start_page"], shard["end_page"])
                local = Path(hf_hub_download(repo, path, repo_type="dataset", token=token))
                if local.stat().st_size != shard["bytes"] or sha256_file(local) != shard["sha256"]:
                    raise RuntimeError(f"first-pass census shard corrupt: {path}")
                for line in verified_shard_lines(local, shard):
                    row = json.loads(line)
                    identifier = str(row["record_id"])
                    if not identifier.startswith("elibrary_ls_question_") or identifier in seen:
                        raise RuntimeError(f"duplicate or non-eLibrary ID in {path}")
                    seen.add(identifier)
                    output.write(line)
        if len(seen) != current_count + target_count:
            raise RuntimeError("full census unique-ID count mismatch")
        new_ids = sum(identifier not in previous_ids for identifier in seen
                      if identifier.startswith("elibrary_ls_question_"))
        removed_ids = len(previous_ids - seen)
        manifest = {
            "snapshot_date": datetime.now(timezone.utc).date().isoformat(),
            "snapshot_timestamp": datetime.now(timezone.utc).isoformat(),
            "record_count": len(seen), "sha256": sha256_file(archive),
            "bytes": archive.stat().st_size, "path": archive_remote,
            "sources": {**base_manifest["sources"], "elibrary_lok_sabha": target_count},
            "base_snapshot": {"path": base_manifest["path"],
                              "sha256": base_manifest["sha256"]},
            "reconciliation_evidence": {
                "method": "two-ascending-passes-normalized-page-hash-agreement",
                "sort": SORT, "page_size": PAGE_SIZE, "pages": expected_pages,
                "first_pass_shards": len(first), "audit_shards": len(audit),
                "new_ids": new_ids, "removed_ids": removed_ids,
            },
            "limitations": "Dated two-pass eLibrary census, not a transactional source snapshot. New tail accessions during the scan are excluded and need incremental refresh. Current-API records remain from the base snapshot. PDF acquisition is separate.",
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        _commit_with_retry(api, repo=repo, operations=[
            CommitOperationAdd(path_in_repo=archive_remote, path_or_fileobj=str(archive)),
            CommitOperationAdd(path_in_repo=manifest_remote,
                               path_or_fileobj=str(manifest_path)),
        ], message=f"Two-pass eLibrary census {root}")
        remote = api.get_paths_info(repo, [archive_remote], repo_type="dataset", expand=True)
        if (len(remote) != 1 or remote[0].size != manifest["bytes"]
                or not remote[0].lfs or remote[0].lfs.sha256 != manifest["sha256"]):
            raise RuntimeError("full eLibrary census failed remote LFS verification")
        saved = json.loads(Path(hf_hub_download(
            repo, manifest_remote, repo_type="dataset", token=token,
            force_download=True)).read_text())
        if saved != manifest:
            raise RuntimeError("full eLibrary census manifest differs from uploaded artifact")
        return manifest


def run(repo: str, root: str, base_root: str, *, max_pages: int,
        max_seconds: int, sleep_seconds: float, token: str,
        api: HfApi | None = None, fetch=search_page) -> dict:
    api = api or HfApi(token=token)
    output_archive, output_manifest = root + "-snapshot.jsonl.gz", root + "-snapshot.json"
    present = set(api.list_repo_files(repo, repo_type="dataset"))
    if output_archive in present or output_manifest in present:
        if not {output_archive, output_manifest} <= present:
            raise RuntimeError("full census output is only partially published")
        saved = json.loads(Path(hf_hub_download(
            repo, output_manifest, repo_type="dataset", token=token)).read_text())
        remote = api.get_paths_info(repo, [output_archive], repo_type="dataset", expand=True)
        if (saved.get("path") != output_archive or len(remote) != 1
                or remote[0].size != saved.get("bytes") or not remote[0].lfs
                or remote[0].lfs.sha256 != saved.get("sha256")):
            raise RuntimeError("existing full census output failed remote verification")
        return {"status": "already_published", "snapshot": output_archive,
                "records": saved["record_count"]}
    first = load_shards(repo, root, "first", token=token, api=api)
    audit = load_shards(repo, root, "audit", token=token, api=api)
    verify_first_shard_files(repo, root, first, token=token, api=api)
    if not first:
        response = fetch(page=0, page_size=PAGE_SIZE, sort=SORT)
        target_count = int(response["_embedded"]["searchResult"]["page"]["totalElements"])
        if target_count < 1:
            raise RuntimeError("eLibrary search returned no records")
    else:
        target_count = first[0]["target_count"]
    target_pages = math.ceil(target_count / PAGE_SIZE)
    if max_pages < min(SHARD_PAGES, target_pages):
        raise ValueError("max_pages is smaller than one durable census shard")
    for row in first + audit:
        if (row["target_count"] != target_count or row["end_page"] >= target_pages
                or row.get("base_root") != base_root):
            raise RuntimeError("inconsistent full census shard target")
    if audit and first and audit[-1]["end_page"] > first[-1]["end_page"]:
        raise RuntimeError("audit pass extends beyond first pass")
    if audit and first[-1]["end_page"] + 1 < target_pages:
        raise RuntimeError("audit pass began before first pass completed")
    for shards in (first, audit):
        if any(right["first_accession"] < left["last_accession"]
               for left, right in zip(shards, shards[1:])):
            raise RuntimeError("census shard accession boundaries reversed")
    started = time.monotonic()
    scanned = 0
    for kind in ("first", "audit"):
        shards = first if kind == "first" else audit
        while not shards or shards[-1]["end_page"] + 1 < target_pages:
            start = shards[-1]["end_page"] + 1 if shards else 0
            end = min(start + SHARD_PAGES - 1, target_pages - 1)
            pages_needed = end - start + 1
            if (scanned + pages_needed > max_pages
                    or time.monotonic() - started >= max_seconds):
                return {"status": "checkpointed", "pass": kind,
                        "pages_this_run": scanned, "target_pages": target_pages}
            matching = None
            if kind == "audit":
                matching = next((s for s in first if s["start_page"] == start), None)
                if matching is None or end != matching["end_page"]:
                    raise RuntimeError("audit shard boundaries differ from first pass")
            prior = shards[-1]["last_accession"] if shards else None
            lines, manifest = scan_shard(fetch, kind=kind, start=start, end=end,
                                         target_count=target_count, prior_accession=prior,
                                         first=matching, sleep_seconds=sleep_seconds)
            manifest["base_root"] = base_root
            saved = publish_shard(api, repo, root, kind, lines, manifest, token=token)
            shards.append(saved)
            scanned += pages_needed
            print(json.dumps({"pass": kind, "through_page": end,
                              "target_pages": target_pages}), flush=True)
    for page, expected in ((0, first[0]["page_hashes"][0]),
                           (target_pages - 1, first[-1]["page_hashes"][-1])):
        lines, _, _ = normalized_page(fetch(page=page, page_size=PAGE_SIZE, sort=SORT),
                                      page, target_count)
        if page_digest(lines) != expected:
            raise RuntimeError(f"eLibrary boundary page {page} changed after audit")
    manifest = assemble_snapshot(api, repo, root, base_root, first, audit, token=token)
    return {"status": "published", "pages_this_run": scanned,
            "snapshot": manifest["path"], "records": manifest["record_count"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--root", required=True,
                        help="Immutable HF scan root, e.g. state/census/full-scan-2026-09-25")
    parser.add_argument("--base-root", required=True)
    parser.add_argument("--max-pages", type=int, default=2500)
    parser.add_argument("--max-seconds", type=int, default=15000)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    args = parser.parse_args()
    if args.max_pages < 1 or args.max_seconds < 1 or args.sleep_seconds < 0:
        parser.error("page/time limits must be positive and sleep non-negative")
    if not re.fullmatch(r"state/census/full-scan-[A-Za-z0-9_-]+", args.root):
        parser.error("--root must be a specific state/census/full-scan-* path")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")
    print(json.dumps(run(args.repo, args.root, args.base_root,
                         max_pages=args.max_pages, max_seconds=args.max_seconds,
                         sleep_seconds=args.sleep_seconds, token=token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
