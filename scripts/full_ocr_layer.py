#!/usr/bin/env python3
"""Publish a resumable, image-only LiteParse OCR layer for every scope page.

This is stored separately from the selected local and Space Bunny layers. When
selected local OCR already used the same image-only LiteParse method, its
verified page artifact can be reused without repeating that expensive OCR.
Run only after a scope's PDF inventory is stable; changing the inventory fails
closed rather than silently aligning old OCR rows to new page positions.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
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
from sansad_pipeline.image_quality import (rendered_ink_metrics, valid_blank_image_evidence,
                                           valid_image_metrics)  # noqa: E402
from sansad_pipeline.liteparse_engine import LiteParseEngine  # noqa: E402
from sansad_pipeline.openrouter import render_page  # noqa: E402
from scripts.cloud_state import _commit_with_retry, sha256_file  # noqa: E402


SHARD_PAGES = 100
BATCH_PAGES = 8


@dataclass(frozen=True)
class Page:
    document_sha256: str
    page_number: int
    raw_path: Path
    route: str | None = None
    artifact_json: Path | None = None
    run_config_json: str | None = None

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
               SELECT r.document_sha256,p.page_number,d.raw_path,p.route,
                      p.artifact_json,r.config_json
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
                  Path(row["raw_path"]), str(row["route"]),
                  Path(row["artifact_json"]), str(row["config_json"])) for row in rows]
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


def completion_marker_path(source: str, key: str) -> str:
    if source == "elibrary":
        return f"state/snapshot-complete/snapshot-complete-{key}.json"
    if source == "current":
        return f"state/complete/session-complete-{key}.json"
    raise ValueError(f"unsupported source: {source}")


def completion_evidence(repo: str, source: str, house: str, parliament: str,
                        session: str, token: str, api: HfApi) -> dict:
    key = f"{house}-p{parliament}-s{session}"
    path = completion_marker_path(source, key)
    raw = Path(hf_hub_download(repo, path, repo_type="dataset", token=token)).read_bytes()
    marker = json.loads(raw)
    inputs = marker.get("inputs") or {}
    complete_field = "snapshot_complete" if source == "elibrary" else "session_complete"
    if (marker.get(complete_field) is not True or inputs.get("source") != source
            or inputs.get("house") != house or inputs.get("parliament") != parliament
            or inputs.get("session") != session or not marker.get("tranche_path")):
        raise RuntimeError(f"scope completion marker lacks matching evidence: {path}")
    tranche = marker["tranche_path"].strip("/")
    required = [f"{tranche}/{name}" for name in
                ("manifest.jsonl", "metadata.json", "SHA256SUMS", "webdataset/shard-00000.tar")]
    found = list(api.get_paths_info(repo, required, repo_type="dataset", expand=False))
    if {item.path for item in found} != set(required):
        raise RuntimeError(f"scope completion marker points to missing tranche files: {path}")
    checkpoint_path = (f"state/checkpoints/{'elibrary-' if source == 'elibrary' else ''}"
                       f"{key}/checkpoint.json")
    checkpoint = json.loads(Path(hf_hub_download(
        repo, checkpoint_path, repo_type="dataset", token=token)).read_text())
    raw_inventory = checkpoint.get("raw_inventory_sha256")
    if checkpoint.get("version") != 3 or not raw_inventory:
        raise RuntimeError(f"scope checkpoint lacks verified PDF inventory: {checkpoint_path}")
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
            "checkpoint_path": checkpoint_path, "raw_inventory_sha256": raw_inventory}


def same_source_pdf_inventory(saved: dict | None, current: dict) -> bool:
    """Allow a republished marker only when its original PDF inventory is unchanged."""
    if not isinstance(saved, dict):
        return False
    fields = ("path", "checkpoint_path", "raw_inventory_sha256")
    return (all(saved.get(field) and saved.get(field) == current.get(field)
                for field in fields)
            and bool(re.fullmatch(r"[0-9a-f]{64}", saved["raw_inventory_sha256"])))


def verify_shard_rows(archive: Path, pages: list[Page], start: int, end: int,
                      version: str) -> dict[str, int]:
    origins = {"selected-local-rasterized-ocr": 0, "sidecar-rasterized-ocr": 0}
    with gzip.open(archive, "rt", encoding="utf-8") as handle:
        for index in range(start, end + 1):
            line = handle.readline()
            if not line:
                raise RuntimeError(f"OCR shard is truncated at index {index}: {archive}")
            row = json.loads(line)
            page = pages[index]
            blank = not str(row.get("text") or "").strip()
            has_visual_metrics = "visually_blank" in row
            blank_evidence = valid_blank_image_evidence(row)
            expected_flags = visual_flags(row, str(row.get("text") or "")) if has_visual_metrics else []
            empty_visible_evidence = (has_visual_metrics and valid_image_metrics(row)
                                      and row["visually_blank"] is False
                                      and row.get("quality_flags") == expected_flags)
            if (row.get("document_sha256") != page.document_sha256
                    or row.get("page_number") != page.page_number
                    or row.get("engine") != "liteparse"
                    or row.get("engine_version") != version
                    or row.get("method") != "image-only-rasterized-ocr"
                    or (blank and not (blank_evidence or empty_visible_evidence))
                    or (has_visual_metrics and not valid_image_metrics(row))
                    or ("quality_flags" in row and row["quality_flags"] != expected_flags)
                    or row.get("origin", "sidecar-rasterized-ocr") not in
                    {"sidecar-rasterized-ocr", "selected-local-rasterized-ocr"}):
                raise RuntimeError(f"OCR shard has invalid page {page.key}: {archive}")
            if (row.get("origin") == "selected-local-rasterized-ocr"
                    and not re.fullmatch(r"[0-9a-f]{64}",
                                         str(row.get("source_artifact_sha256") or ""))):
                raise RuntimeError(f"OCR shard lacks reusable artifact SHA-256: {archive}")
            origins[row.get("origin", "sidecar-rasterized-ocr")] += 1
        if handle.readline():
            raise RuntimeError(f"OCR shard has extra rows: {archive}")
    return origins


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
        origins = verify_shard_rows(local, pages, start, end, version)
        if "origin_counts" in manifest and manifest["origin_counts"] != origins:
            raise RuntimeError(f"OCR shard origin counts mismatch: {path}")
        result.append(manifest)
        expected_start = end + 1
    return result


def page_image_metrics(page: Page, dpi: int) -> dict:
    with tempfile.TemporaryDirectory() as directory:
        image = render_page(page.raw_path, page.page_number, Path(directory), dpi)
        metrics = rendered_ink_metrics(image)
    if not valid_image_metrics(metrics):
        raise RuntimeError(f"invalid rendered page metrics for {page.key}")
    return metrics


def visual_flags(metrics: dict, text: str) -> list[str]:
    if metrics["visually_blank"] and text.strip():
        return ["ocr-nonempty-on-visually-blank-page"]
    if not metrics["visually_blank"] and not text.strip():
        return ["ocr-empty-on-visibly-nonblank-page"]
    return []


def reusable_ocr_row(engine: LiteParseEngine, page: Page) -> dict | None:
    if page.route != "ocr" or page.artifact_json is None or page.run_config_json is None:
        return None
    try:
        run_config = json.loads(page.run_config_json)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid extraction config for {page.key}") from exc
    if not isinstance(run_config, dict):
        raise RuntimeError(f"invalid extraction config for {page.key}")
    if run_config.get("liteparse") != asdict(engine.config.liteparse):
        return None
    try:
        artifact_bytes = page.artifact_json.read_bytes()
        artifact = json.loads(artifact_bytes)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"selected OCR artifact is unavailable or invalid for {page.key}") from exc
    if (not isinstance(artifact, dict)
            or artifact.get("document_sha256") != page.document_sha256
            or artifact.get("page_number") != page.page_number
            or artifact.get("route") != "ocr"
            or artifact.get("engine") != engine.name
            or artifact.get("engine_version") != engine.version):
        raise RuntimeError(f"selected OCR artifact identity mismatch for {page.key}")
    if "full-page-image" not in artifact.get("route_reasons", []):
        return None
    text = str(artifact.get("text") or "")
    metrics = page_image_metrics(page, engine.config.liteparse.full_page_image_dpi)
    return {
        "document_sha256": page.document_sha256,
        "page_number": page.page_number,
        "engine": "liteparse", "engine_version": engine.version,
        "method": "image-only-rasterized-ocr",
        "origin": "selected-local-rasterized-ocr",
        "text": text, "markdown": artifact.get("markdown") or "",
        "mean_confidence": artifact.get("mean_confidence"),
        "source_artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        "quality_flags": visual_flags(metrics, text), **metrics,
    }


def ocr_rows(engine: LiteParseEngine, pages: list[Page]) -> list[dict]:
    rows: list[dict] = []
    reusable = [reusable_ocr_row(engine, page) for page in pages]
    index = 0
    while index < len(pages):
        if reusable[index] is not None:
            rows.append(reusable[index])
            index += 1
            continue
        first = pages[index]
        same_pdf = [first]
        while (index + len(same_pdf) < len(pages) and len(same_pdf) < BATCH_PAGES
               and pages[index + len(same_pdf)].document_sha256 == first.document_sha256
               and reusable[index + len(same_pdf)] is None):
            same_pdf.append(pages[index + len(same_pdf)])
        extracted = engine.extract(first.raw_path, ocr=True,
                                   target_pages=[page.page_number for page in same_pdf],
                                   rasterize=True)
        by_number = {page.page_number: page for page in extracted}
        if len(by_number) != len(same_pdf):
            raise RuntimeError(f"LiteParse OCR page count mismatch for {first.document_sha256}")
        for page in same_pdf:
            item = by_number[page.page_number]
            metrics = getattr(item, "visual_quality", None)
            if metrics is None:
                metrics = page_image_metrics(page, engine.config.liteparse.full_page_image_dpi)
            elif not valid_image_metrics(metrics):
                raise RuntimeError(f"invalid LiteParse screenshot metrics for {page.key}")
            rows.append({
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
                "engine": "liteparse", "engine_version": engine.version,
                "method": "image-only-rasterized-ocr",
                "origin": "sidecar-rasterized-ocr",
                "text": item.text, "markdown": item.markdown,
                "mean_confidence": item.mean_confidence,
                "quality_flags": visual_flags(metrics, item.text), **metrics,
            })
        index += len(same_pdf)
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
        origins = verify_shard_rows(archive, requested, 0, len(requested) - 1,
                                    engine.version)
        manifest = {
            "path": archive_path, "start_index": start, "end_index": end,
            "first_key": requested[0].key, "last_key": requested[-1].key,
            "record_count": len(rows), "inventory_sha256": inventory,
            "engine": "liteparse", "engine_version": engine.version,
            "method": "image-only-rasterized-ocr",
            "origin_counts": origins,
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


def run(repo: str, source: str, house: str, parliament: str, session: str, *, max_pages: int,
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
    marker = completion_evidence(repo, source, house, parliament, session, token, api)
    shards = completed_shards(api, repo, root, pages, inventory, engine.version, token)
    next_index = shards[-1]["end_index"] + 1 if shards else 0
    started = time.monotonic()
    processed = 0
    while next_index < len(pages):
        end = min(next_index + SHARD_PAGES, len(pages))
        if processed + end - next_index > max_pages or time.monotonic() - started >= max_seconds:
            break
        indexed = list(enumerate(pages[next_index:end], next_index))
        shards.append(publish_shard(api, repo, root, indexed, inventory, engine, token))
        processed += len(indexed)
        next_index = end
        print(json.dumps({"ocr_pages_complete": next_index, "scope_pages": len(pages)}),
              flush=True)
    status = "complete" if next_index == len(pages) else "checkpointed"
    if status == "complete":
        if (not shards or shards[-1]["end_index"] + 1 != len(pages)):
            raise RuntimeError("OCR coverage is incomplete despite reaching the last page")
        completion = {
            "root": root, "source": source, "scope": key,
            "pages": len(pages), "inventory_sha256": inventory,
            "engine": "liteparse", "engine_version": engine.version,
            "method": "image-only-rasterized-ocr",
            "completion_marker": marker,
            "shards": len(shards),
        }
        completion_path = root + "/complete.json"
        existing = set(api.list_repo_files(repo, repo_type="dataset"))
        upload_completion = completion_path not in existing
        if completion_path in existing:
            saved = json.loads(Path(hf_hub_download(
                repo, completion_path, repo_type="dataset", token=token)).read_text())
            if saved != completion:
                old_identity = {key: value for key, value in saved.items()
                                if key != "completion_marker"}
                new_identity = {key: value for key, value in completion.items()
                                if key != "completion_marker"}
                if (old_identity != new_identity or not same_source_pdf_inventory(
                        saved.get("completion_marker"), marker)):
                    raise RuntimeError("existing OCR completion marker differs from verified inventory")
        if upload_completion:
            _commit_with_retry(api, repo=repo, operations=[CommitOperationAdd(
                path_in_repo=completion_path,
                path_or_fileobj=json.dumps(completion, ensure_ascii=False, indent=2).encode("utf-8"),
            )], message=f"Complete full LiteParse OCR {key}")
            saved = json.loads(Path(hf_hub_download(
                repo, completion_path, repo_type="dataset", token=token,
                force_download=True)).read_text())
            if saved != completion:
                raise RuntimeError("OCR completion marker failed remote verification")
    return {"status": status, "root": root, "pages": len(pages),
            "ocr_pages_complete": next_index, "pages_this_run": processed,
            "inventory_sha256": inventory}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--source", choices=("current", "elibrary"), required=True)
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
    print(json.dumps(run(args.repo, args.source, args.house, args.parliament, args.session,
                         max_pages=args.max_pages, max_seconds=args.max_seconds,
                         token=token)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
