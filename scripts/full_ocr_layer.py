#!/usr/bin/env python3
"""Publish a resumable, image-only LiteParse OCR layer for every scope page.

This is deliberately independent of the selected local and Space Bunny layers.
Run only after a scope's PDF inventory is stable; changing the inventory fails
closed rather than silently aligning old OCR rows to new page positions.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from huggingface_hub import CommitOperationAdd, HfApi, hf_hub_download

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sansad_pipeline.config import load_config  # noqa: E402
from sansad_pipeline.liteparse_engine import LiteParseEngine  # noqa: E402
from scripts.cloud_state import _commit_with_retry, sha256_file  # noqa: E402


SHARD_PAGES = 100
BATCH_PAGES = 8


@dataclass(frozen=True)
class Page:
    document_sha256: str
    page_number: int
    raw_path: Path

    @property
    def key(self) -> str:
        return f"{self.document_sha256}:{self.page_number}"


def scope_pages(database: Path, house: str, parliament: str, session: str) -> list[Page]:
    if not database.is_file():
        raise RuntimeError(f"restored checkpoint has no database: {database}")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        census = connection.execute(
            """SELECT COUNT(*) AS records,
                      COUNT(DISTINCT CASE WHEN acquisition_status='downloaded'
                           THEN document_sha256 END) AS downloaded_pdfs,
                      SUM(CASE WHEN acquisition_status!='downloaded' THEN 1 ELSE 0 END)
                           AS non_downloaded_records
               FROM census_records WHERE source_type='questions_answers'
                 AND house=? AND parliament_number=? AND session=?""",
            (house, parliament, session),
        ).fetchone()
        rows = connection.execute(
            """WITH scope_docs AS (
                 SELECT DISTINCT document_sha256 FROM census_records
                 WHERE source_type='questions_answers' AND house=?
                   AND parliament_number=? AND session=?
                   AND document_sha256 IS NOT NULL
               )
               SELECT r.document_sha256,p.page_number,d.raw_path
               FROM scope_docs s JOIN runs r ON r.document_sha256=s.document_sha256
               JOIN pages p ON p.run_id=r.id
               JOIN documents d ON d.sha256=r.document_sha256
               WHERE r.status='complete'
                 AND r.id=(SELECT MAX(r2.id) FROM runs r2
                           WHERE r2.document_sha256=r.document_sha256
                             AND r2.status='complete')
               ORDER BY r.document_sha256,p.page_number""",
            (house, parliament, session),
        ).fetchall()
    finally:
        connection.close()
    pages = [Page(str(row["document_sha256"]), int(row["page_number"]),
                  Path(row["raw_path"])) for row in rows]
    if not pages:
        raise RuntimeError("scope has no completed extracted pages")
    if len({page.key for page in pages}) != len(pages):
        raise RuntimeError("scope page inventory contains duplicate PDF/page keys")
    if census["non_downloaded_records"] or len({page.document_sha256 for page in pages}) != census["downloaded_pdfs"]:
        raise RuntimeError("scope PDF acquisition/extraction is incomplete; full OCR inventory is not stable")
    for page in pages:
        if not page.raw_path.is_file():
            raise RuntimeError(f"restored original PDF is missing: {page.raw_path}")
    return pages


def inventory_sha256(pages: list[Page]) -> str:
    value = "".join(page.key + "\n" for page in pages)
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def shard_paths(root: str, start: int, end: int) -> tuple[str, str]:
    stem = f"{root}/part-{start:08d}-{end:08d}"
    return stem + ".jsonl.gz", stem + ".json"


def verify_shard_rows(archive: Path, pages: list[Page], start: int, end: int,
                      version: str) -> None:
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        for index in range(start, end + 1):
            line = handle.readline()
            if not line:
                raise RuntimeError(f"OCR shard is truncated at index {index}: {archive}")
            row = json.loads(line)
            page = pages[index]
            if (row.get("document_sha256") != page.document_sha256
                    or row.get("page_number") != page.page_number
                    or row.get("engine") != "liteparse"
                    or row.get("engine_version") != version
                    or row.get("method") != "image-only-rasterized-ocr"
                    or not str(row.get("text") or "").strip()):
                raise RuntimeError(f"OCR shard has invalid page {page.key}: {archive}")
        if handle.readline():
            raise RuntimeError(f"OCR shard has extra rows: {archive}")


def completed_shards(api: HfApi, repo: str, root: str, pages: list[Page],
                     inventory: str, version: str, token: str) -> list[dict]:
    files = set(api.list_repo_files(repo, repo_type="dataset"))
    prefix = root + "/part-"
    manifests = sorted(path for path in files if path.startswith(prefix)
                       and path.endswith(".json"))
    if any(path.startswith(prefix) and path.endswith(".jsonl.gz")
           and path[:-9] + ".json" not in files for path in files):
        raise RuntimeError("OCR data shard exists without its manifest")
    expected_start = 0
    result: list[dict] = []
    for path in manifests:
        manifest = json.loads(Path(hf_hub_download(
            repo, path, repo_type="dataset", token=token)).read_text())
        start, end = manifest.get("start_index"), manifest.get("end_index")
        if (not isinstance(start, int) or not isinstance(end, int)
                or start != expected_start or end < start
                or end - start + 1 > SHARD_PAGES or end >= len(pages)
                or path != shard_paths(root, start, end)[1]
                or manifest.get("inventory_sha256") != inventory
                or manifest.get("engine") != "liteparse"
                or manifest.get("engine_version") != version
                or manifest.get("first_key") != pages[start].key
                or manifest.get("last_key") != pages[end].key
                or manifest.get("record_count") != end - start + 1):
            raise RuntimeError(f"invalid or stale OCR shard manifest: {path}")
        archive_path, _ = shard_paths(root, start, end)
        if archive_path not in files or manifest.get("path") != archive_path:
            raise RuntimeError(f"OCR shard is missing its archive: {path}")
        remote = list(api.get_paths_info(repo, [archive_path], repo_type="dataset", expand=True))
        if len(remote) != 1 or remote[0].size != manifest.get("bytes"):
            raise RuntimeError(f"OCR shard remote size mismatch: {archive_path}")
        if remote[0].lfs:
            if remote[0].lfs.sha256 != manifest.get("sha256"):
                raise RuntimeError(f"OCR shard remote hash mismatch: {archive_path}")
        local = Path(hf_hub_download(repo, archive_path, repo_type="dataset", token=token))
        if local.stat().st_size != manifest.get("bytes") or sha256_file(local) != manifest.get("sha256"):
            raise RuntimeError(f"OCR shard local checksum mismatch: {archive_path}")
        verify_shard_rows(local, pages, start, end, version)
        result.append(manifest)
        expected_start = end + 1
    return result


def ocr_rows(engine: LiteParseEngine, pages: list[Page]) -> list[dict]:
    rows: list[dict] = []
    for offset in range(0, len(pages), BATCH_PAGES):
        batch = pages[offset:offset + BATCH_PAGES]
        # Keep the image-only PDF small and never mix two original PDFs.
        first = batch[0]
        same_pdf = [first]
        for page in batch[1:]:
            if page.document_sha256 != first.document_sha256:
                break
            same_pdf.append(page)
        extracted = engine.extract(first.raw_path, ocr=True,
                                   target_pages=[page.page_number for page in same_pdf],
                                   rasterize=True)
        by_number = {page.page_number: page for page in extracted}
        if len(by_number) != len(same_pdf):
            raise RuntimeError(f"LiteParse OCR page count mismatch for {first.document_sha256}")
        for page in same_pdf:
            item = by_number[page.page_number]
            if not item.text.strip():
                raise RuntimeError(f"LiteParse OCR returned empty text for {page.key}")
            rows.append({
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
                "engine": "liteparse", "engine_version": engine.version,
                "method": "image-only-rasterized-ocr",
                "text": item.text, "markdown": item.markdown,
                "mean_confidence": item.mean_confidence,
            })
        # A batch can cross a PDF boundary. Process its remainder next.
        if len(same_pdf) != len(batch):
            rows.extend(ocr_rows(engine, batch[len(same_pdf):]))
    if len(rows) != len(pages):
        raise RuntimeError("OCR shard did not cover every requested PDF/page")
    return rows


def publish_shard(api: HfApi, repo: str, root: str, pages: list[tuple[int, Page]],
                  inventory: str, engine: LiteParseEngine, token: str) -> dict:
    start, end = pages[0][0], pages[-1][0]
    requested = [page for _, page in pages]
    rows = ocr_rows(engine, requested)
    archive_path, manifest_path = shard_paths(root, start, end)
    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "ocr.jsonl.gz"
        manifest_file = Path(directory) / "ocr.json"
        with gzip.open(archive, "wt", encoding="utf-8", compresslevel=6) as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        verify_shard_rows(archive, requested, 0, len(requested) - 1, engine.version)
        manifest = {
            "path": archive_path, "start_index": start, "end_index": end,
            "first_key": requested[0].key, "last_key": requested[-1].key,
            "record_count": len(rows), "inventory_sha256": inventory,
            "engine": "liteparse", "engine_version": engine.version,
            "method": "image-only-rasterized-ocr",
            "sha256": sha256_file(archive), "bytes": archive.stat().st_size,
        }
        manifest_file.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        _commit_with_retry(api, repo=repo, operations=[
            CommitOperationAdd(path_in_repo=archive_path, path_or_fileobj=str(archive)),
            CommitOperationAdd(path_in_repo=manifest_path, path_or_fileobj=str(manifest_file)),
        ], message=f"Full LiteParse OCR pages {start}-{end}")
        remote = list(api.get_paths_info(repo, [archive_path], repo_type="dataset", expand=True))
        if len(remote) != 1 or remote[0].size != manifest["bytes"]:
            raise RuntimeError(f"uploaded OCR shard failed size verification: {archive_path}")
        if remote[0].lfs:
            if remote[0].lfs.sha256 != manifest["sha256"]:
                raise RuntimeError(f"uploaded OCR shard failed LFS verification: {archive_path}")
        elif sha256_file(Path(hf_hub_download(repo, archive_path,
                                             repo_type="dataset", token=token))) != manifest["sha256"]:
            raise RuntimeError(f"uploaded OCR shard failed Git hash verification: {archive_path}")
        saved = json.loads(Path(hf_hub_download(repo, manifest_path, repo_type="dataset",
                                               token=token, force_download=True)).read_text())
        if saved != manifest:
            raise RuntimeError(f"uploaded OCR shard manifest differs: {manifest_path}")
        return manifest


def run(repo: str, house: str, parliament: str, session: str, *, max_pages: int,
        max_seconds: int, token: str, database: Path = Path("data/pipeline.sqlite3"),
        api: HfApi | None = None) -> dict:
    config = load_config(Path("pipeline.toml"))
    engine = LiteParseEngine(config)
    pages = scope_pages(database, house, parliament, session)
    inventory = inventory_sha256(pages)
    key = f"{house}-p{parliament}-s{session}"
    root = (f"layers/full-ocr/{key}/inventory-{inventory[:16]}/liteparse-{engine.version}-"
            f"{config.liteparse.language}-{config.liteparse.full_page_image_dpi}dpi")
    api = api or HfApi(token=token)
    shards = completed_shards(api, repo, root, pages, inventory, engine.version, token)
    next_index = shards[-1]["end_index"] + 1 if shards else 0
    started = time.monotonic()
    processed = 0
    while next_index < len(pages):
        end = min(next_index + SHARD_PAGES, len(pages))
        if processed + end - next_index > max_pages or time.monotonic() - started >= max_seconds:
            break
        indexed = list(enumerate(pages[next_index:end], next_index))
        publish_shard(api, repo, root, indexed, inventory, engine, token)
        processed += len(indexed)
        next_index = end
        print(json.dumps({"ocr_pages_complete": next_index, "scope_pages": len(pages)}),
              flush=True)
    status = "complete" if next_index == len(pages) else "checkpointed"
    return {"status": status, "root": root, "pages": len(pages),
            "ocr_pages_complete": next_index, "pages_this_run": processed,
            "inventory_sha256": inventory}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--house", required=True)
    parser.add_argument("--parliament", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=15000)
    args = parser.parse_args()
    if args.max_pages < SHARD_PAGES or args.max_seconds < 1:
        parser.error("max-pages must fit one 100-page shard and max-seconds must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.parliament) or not re.fullmatch(
        r"[A-Za-z0-9_-]+", args.session
    ):
        parser.error("parliament and session labels must be path-safe")
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("HF_TOKEN is required")
    print(json.dumps(run(args.repo, args.house, args.parliament, args.session,
                         max_pages=args.max_pages, max_seconds=args.max_seconds,
                         token=token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
