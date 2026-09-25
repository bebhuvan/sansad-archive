#!/usr/bin/env python3
"""Backfill immutable, all-page image measurements for published PDF scopes.

This is a bounded cloud sidecar for scopes whose older OCR/model shards lack
all-page visual evidence. It never edits the original PDFs or text layers.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import tempfile
import time
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

from sansad_pipeline.config import load_config
from sansad_pipeline.image_quality import rendered_ink_metrics, valid_image_metrics
from sansad_pipeline.openrouter import render_page
from scripts.cloud_state import _commit_with_retry, sha256_file
from scripts.full_ocr_layer import Page, completion_evidence, inventory_sha256, scope_pages


SHARD_PAGES = 100
METHOD = "liteparse-rendered-page-ink-v1"


def shard_paths(root: str, start: int, end: int) -> tuple[str, str]:
    stem = f"{root}/part-{start:08d}-{end:08d}"
    return stem + ".jsonl.gz", stem + ".json"


def verify_rows(archive: Path, pages: list[Page], start: int, end: int,
                dpi: int) -> dict[str, int]:
    blank = 0
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        for index in range(start, end + 1):
            line = handle.readline()
            if not line:
                raise RuntimeError(f"visual shard is truncated at index {index}: {archive}")
            row = json.loads(line)
            page = pages[index]
            if (row.get("document_sha256") != page.document_sha256
                    or row.get("page_number") != page.page_number
                    or row.get("method") != METHOD
                    or row.get("dpi") != dpi
                    or not valid_image_metrics(row)):
                raise RuntimeError(f"visual shard has invalid page {page.key}: {archive}")
            blank += row["visually_blank"]
        if handle.readline():
            raise RuntimeError(f"visual shard has extra rows: {archive}")
    return {"visually_blank": blank, "visibly_nonblank": end - start + 1 - blank}


def completed_shards(api: HfApi, repo: str, root: str, pages: list[Page],
                     inventory: str, dpi: int, token: str) -> list[dict]:
    files = set(api.list_repo_files(repo, repo_type="dataset"))
    prefix = root + "/part-"
    manifests = sorted(path for path in files if path.startswith(prefix)
                       and path.endswith(".json"))
    if any(path.startswith(prefix) and path.endswith(".jsonl.gz")
           and path[:-9] + ".json" not in files for path in files):
        raise RuntimeError("visual data shard exists without its manifest")
    expected_start = 0
    result = []
    for path in manifests:
        manifest = json.loads(Path(hf_hub_download(
            repo, path, repo_type="dataset", token=token,
        )).read_text(encoding="utf-8"))
        start, end = manifest.get("start_index"), manifest.get("end_index")
        if (type(start) is not int or type(end) is not int
                or start != expected_start or end < start or end - start + 1 > SHARD_PAGES
                or end >= len(pages) or path != shard_paths(root, start, end)[1]
                or manifest.get("inventory_sha256") != inventory
                or manifest.get("method") != METHOD or manifest.get("dpi") != dpi
                or manifest.get("first_key") != pages[start].key
                or manifest.get("last_key") != pages[end].key
                or manifest.get("record_count") != end - start + 1):
            raise RuntimeError(f"invalid or stale visual shard manifest: {path}")
        archive_path, _ = shard_paths(root, start, end)
        if archive_path not in files or manifest.get("path") != archive_path:
            raise RuntimeError(f"visual shard is missing its archive: {path}")
        remote = list(api.get_paths_info(repo, [archive_path], repo_type="dataset", expand=True))
        if len(remote) != 1 or remote[0].size != manifest.get("bytes"):
            raise RuntimeError(f"visual shard remote size mismatch: {archive_path}")
        if remote[0].lfs and remote[0].lfs.sha256 != manifest.get("sha256"):
            raise RuntimeError(f"visual shard remote hash mismatch: {archive_path}")
        local = Path(hf_hub_download(repo, archive_path, repo_type="dataset", token=token))
        if local.stat().st_size != manifest.get("bytes") or sha256_file(local) != manifest.get("sha256"):
            raise RuntimeError(f"visual shard local checksum mismatch: {archive_path}")
        counts = verify_rows(local, pages, start, end, dpi)
        if manifest.get("visual_counts") != counts:
            raise RuntimeError(f"visual shard counts mismatch: {path}")
        result.append(manifest)
        expected_start = end + 1
    return result


def page_row(page: Page, dpi: int) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        image = render_page(page.raw_path, page.page_number, Path(directory), dpi)
        metrics = rendered_ink_metrics(image)
    if not valid_image_metrics(metrics):
        raise RuntimeError(f"invalid rendered image measurements for {page.key}")
    return {"document_sha256": page.document_sha256,
            "page_number": page.page_number, "method": METHOD, "dpi": dpi,
            **metrics}


def publish_shard(api: HfApi, repo: str, root: str, indexed: list[tuple[int, Page]],
                  inventory: str, dpi: int, token: str) -> dict:
    start, end = indexed[0][0], indexed[-1][0]
    pages = [page for _, page in indexed]
    archive_path, manifest_path = shard_paths(root, start, end)
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "visual.jsonl.gz"
        manifest_file = Path(directory) / "visual.json"
        with gzip.open(archive, "wt", encoding="utf-8", compresslevel=6) as handle:
            for page in pages:
                handle.write(json.dumps(page_row(page, dpi), sort_keys=True) + "\n")
        counts = verify_rows(archive, pages, 0, len(pages) - 1, dpi)
        manifest = {
            "path": archive_path, "start_index": start, "end_index": end,
            "first_key": pages[0].key, "last_key": pages[-1].key,
            "record_count": len(pages), "inventory_sha256": inventory,
            "method": METHOD, "dpi": dpi, "visual_counts": counts,
            "sha256": sha256_file(archive), "bytes": archive.stat().st_size,
        }
        manifest_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _commit_with_retry(api, repo=repo, operations=[
            CommitOperationAdd(path_in_repo=archive_path, path_or_fileobj=str(archive)),
            CommitOperationAdd(path_in_repo=manifest_path, path_or_fileobj=str(manifest_file)),
        ], message=f"Visual page evidence {start}-{end}")
        remote = list(api.get_paths_info(repo, [archive_path], repo_type="dataset", expand=True))
        if len(remote) != 1 or remote[0].size != manifest["bytes"]:
            raise RuntimeError(f"uploaded visual shard failed size verification: {archive_path}")
        if remote[0].lfs:
            if remote[0].lfs.sha256 != manifest["sha256"]:
                raise RuntimeError(f"uploaded visual shard failed LFS verification: {archive_path}")
        elif sha256_file(Path(hf_hub_download(repo, archive_path, repo_type="dataset",
                                             token=token))) != manifest["sha256"]:
            raise RuntimeError(f"uploaded visual shard failed Git hash verification: {archive_path}")
        saved = json.loads(Path(hf_hub_download(
            repo, manifest_path, repo_type="dataset", token=token,
            force_download=True,
        )).read_text(encoding="utf-8"))
        if saved != manifest:
            raise RuntimeError(f"uploaded visual shard manifest differs: {manifest_path}")
        return manifest


def run(repo: str, source: str, house: str, parliament: str, session: str,
        *, max_pages: int, max_seconds: int, token: str,
        database: Path = Path("data/pipeline.sqlite3"), api: HfApi | None = None) -> dict:
    config = load_config(Path("pipeline.toml"))
    dpi = config.liteparse.full_page_image_dpi
    pages = scope_pages(database, house, parliament, session)
    inventory = inventory_sha256(pages)
    key = f"{house}-p{parliament}-s{session}"
    root = (f"layers/visual-evidence/{key}/inventory-{inventory[:16]}/"
            f"render-{dpi}dpi-v1")
    api = api or HfApi(token=token)
    marker = completion_evidence(repo, source, house, parliament, session, token, api)
    shards = completed_shards(api, repo, root, pages, inventory, dpi, token)
    next_index = shards[-1]["end_index"] + 1 if shards else 0
    started = time.monotonic()
    processed = 0
    while next_index < len(pages):
        end = min(next_index + SHARD_PAGES, len(pages))
        if processed + end - next_index > max_pages or time.monotonic() - started >= max_seconds:
            break
        indexed = list(enumerate(pages[next_index:end], next_index))
        shards.append(publish_shard(api, repo, root, indexed, inventory, dpi, token))
        processed += len(indexed)
        next_index = end
        print(json.dumps({"visual_pages_complete": next_index,
                          "scope_pages": len(pages)}), flush=True)
    status = "complete" if next_index == len(pages) else "checkpointed"
    if status == "complete":
        completion = {
            "root": root, "source": source, "scope": key,
            "pages": len(pages), "inventory_sha256": inventory,
            "method": METHOD, "dpi": dpi,
            "completion_marker": marker, "shards": len(shards),
        }
        path = root + "/complete.json"
        existing = set(api.list_repo_files(repo, repo_type="dataset"))
        upload = path not in existing
        if path in existing:
            saved = json.loads(Path(hf_hub_download(
                repo, path, repo_type="dataset", token=token,
            )).read_text(encoding="utf-8"))
            if saved != completion:
                raise RuntimeError("existing visual completion marker differs from verified inventory")
        if upload:
            _commit_with_retry(api, repo=repo, operations=[CommitOperationAdd(
                path_in_repo=path,
                path_or_fileobj=json.dumps(completion, indent=2).encode("utf-8"),
            )], message=f"Complete all-page visual evidence {key}")
            saved = json.loads(Path(hf_hub_download(
                repo, path, repo_type="dataset", token=token,
                force_download=True,
            )).read_text(encoding="utf-8"))
            if saved != completion:
                raise RuntimeError("visual completion marker failed remote verification")
    return {"status": status, "root": root, "pages": len(pages),
            "visual_pages_complete": next_index, "pages_this_run": processed,
            "inventory_sha256": inventory}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source", choices=("current", "elibrary"), required=True)
    parser.add_argument("--house", choices=("lok_sabha", "rajya_sabha"), required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=15000)
    args = parser.parse_args()
    if (args.max_pages < SHARD_PAGES or args.max_seconds < 1
            or not re.fullmatch(r"[A-Za-z0-9_-]+", args.parliament)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", args.session)):
        parser.error("invalid shard budget or scope label")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")
    print(json.dumps(run(args.repo, args.source, args.house, args.parliament,
                         args.session, max_pages=args.max_pages,
                         max_seconds=args.max_seconds, token=token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
