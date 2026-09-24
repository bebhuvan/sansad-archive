#!/usr/bin/env python3
"""Checkpoint and restore pipeline state to a Hugging Face dataset repository.

The GitHub Actions runner is ephemeral, so an interrupted session must be able
to resume without repeating model calls. The checkpoint contains the SQLite
state, the immutable raw PDFs, extraction artifacts and event logs. Rendered
page images are excluded because they are large and regenerable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data"

EXCLUDE_NAMES = ("data/tmp", "data/exports", "data/publications")
EXCLUDE_SUFFIXES = (".png", ".jpg", ".jpeg")
CHECKPOINT_NAME = "checkpoint.tar.zst"  # legacy v1 archive
STATE_NAME = "state.tar.zst"
RAW_NAME = "raw.tar.zst"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _checkpoint_sqlite(path: Path) -> None:
    if not path.is_file():
        return
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _iter_state_files() -> list[tuple[Path, str]]:
    items: list[tuple[Path, str]] = []
    for relative in ("data/pipeline.sqlite3",):
        path = PROJECT_ROOT / relative
        if path.is_file():
            items.append((path, relative))
    for directory in ("data/raw", "data/artifacts", "data/logs"):
        root = PROJECT_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(PROJECT_ROOT).as_posix()
            if any(relative.startswith(prefix) for prefix in EXCLUDE_NAMES):
                continue
            if path.name.endswith(EXCLUDE_SUFFIXES):
                continue
            items.append((path, relative))
    return items


def _raw_index(files: list[tuple[Path, str]]) -> dict[str, dict]:
    """Manifest of immutable originals, independently verifiable on restore."""
    return {
        relative: {"bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path, relative in files
    }


def _raw_inventory(index: dict[str, dict]) -> str:
    digest = hashlib.sha256()
    for relative, info in sorted(index.items()):
        digest.update(f"{relative}\0{info['bytes']}\0{info['sha256']}\n".encode())
    return digest.hexdigest()


def _legacy_v2_index(repo: str, info: dict, token: str | None) -> dict[str, dict]:
    """Verify V2 originals once so migration cannot mask changed PDF bytes."""
    import zstandard
    from huggingface_hub import hf_hub_download

    archive_path = Path(hf_hub_download(
        repo_id=repo, filename=info["path"], repo_type="dataset", token=token,
    ))
    if archive_path.stat().st_size != info["bytes"] or sha256_file(archive_path) != info["sha256"]:
        raise RuntimeError("legacy raw archive failed size or SHA-256 verification")
    with archive_path.open("rb") as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                index = {}
                for member in archive:
                    if not member.isfile():
                        continue
                    digest = hashlib.sha256()
                    with archive.extractfile(member) as raw:
                        for block in iter(lambda: raw.read(1024 * 1024), b""):
                            digest.update(block)
                    index[member.name] = {"bytes": member.size, "sha256": digest.hexdigest()}
                return index


def _write_archive(path: Path, files: list[tuple[Path, str]]) -> dict:
    import zstandard

    compressor = zstandard.ZstdCompressor(level=3)
    with path.open("wb") as target:
        with compressor.stream_writer(target) as stream:
            with tarfile.open(fileobj=stream, mode="w|") as archive:
                for source, relative in files:
                    archive.add(source, arcname=relative)
    return {"bytes": path.stat().st_size, "sha256": sha256_file(path),
            "files": len(files)}


def _existing_manifest(repo: str, path_in_repo: str, token: str | None) -> dict | None:
    from huggingface_hub import hf_hub_download

    try:
        path = hf_hub_download(
            repo_id=repo, filename=f"{path_in_repo}/checkpoint.json",
            repo_type="dataset", token=token,
        )
    except Exception as error:
        if getattr(getattr(error, "response", None), "status_code", None) == 404:
            return None
        raise
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _commit_with_retry(api, *, repo: str, operations: list, message: str):
    """Keep an ephemeral runner alive through a temporary Hub commit limit."""
    for attempt in range(11):
        try:
            return api.create_commit(
                repo_id=repo, repo_type="dataset", operations=operations,
                commit_message=message,
            )
        except Exception as error:
            response = getattr(error, "response", None)
            status = getattr(response, "status_code", None)
            if status not in {429, 500, 502, 503, 504} or attempt >= (10 if status == 429 else 4):
                raise
            wait = min(600, 30 * 2**attempt)
            retry_after = response.headers.get("Retry-After") if response else None
            if retry_after and retry_after.isdecimal():
                wait = min(600, max(wait, int(retry_after)))
            print(f"Hub commit HTTP {status}; retrying in {wait}s", file=sys.stderr)
            time.sleep(wait)


def save(repo: str, path_in_repo: str, *, token: str | None) -> dict:
    from huggingface_hub import CommitOperationAdd, HfApi

    DATA.mkdir(parents=True, exist_ok=True)
    _checkpoint_sqlite(DATA / "pipeline.sqlite3")
    files = _iter_state_files()
    if not files:
        raise SystemExit("no pipeline state to checkpoint; run the pipeline first")
    path_in_repo = path_in_repo.strip("/")
    raw_files = [(path, relative) for path, relative in files if relative.startswith("data/raw/")]
    state_files = [(path, relative) for path, relative in files if not relative.startswith("data/raw/")]
    api = HfApi(token=token)
    api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
    prior = _existing_manifest(repo, path_in_repo, token)
    index = _raw_index(raw_files)
    prior_version = (prior or {}).get("version")
    shards: list[dict] = []
    if prior_version == 3:
        previous_index = prior.get("raw_index") or {}
        shards = list(prior.get("raw_shards") or [])
        if previous_index and not shards:
            raise RuntimeError("v3 checkpoint has a raw index but no shards")
        for relative, info in previous_index.items():
            if index.get(relative) != info:
                raise RuntimeError(f"previously checkpointed original changed or disappeared: {relative}")
        known = set(previous_index)
    elif prior_version == 2 and prior.get("raw"):
        old = prior["raw"]
        shards = [old]
        if old.get("inventory_sha256") == _raw_inventory(index):
            known = set(index)
        else:
            old_index = _legacy_v2_index(repo, old, token)
            for relative, info in old_index.items():
                if index.get(relative) != info:
                    raise RuntimeError(
                        f"previously checkpointed original changed or disappeared during v2 migration: {relative}"
                    )
            known = set(old_index)
    else:
        known = set()
    additions = [(path, relative) for path, relative in raw_files if relative not in known]
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data") as temp_dir:
        temp = Path(temp_dir)
        state_path = temp / STATE_NAME
        state_info = _write_archive(state_path, state_files)
        state_info["path"] = f"{path_in_repo}/{STATE_NAME}"
        operations = [CommitOperationAdd(
            path_in_repo=state_info["path"], path_or_fileobj=str(state_path)
        )]
        if additions:
            raw_path = temp / RAW_NAME
            raw_info = _write_archive(raw_path, additions)
            raw_info["path"] = f"{path_in_repo}/raw/{raw_info['sha256']}.tar.zst"
            shards.append(raw_info)
            operations.append(CommitOperationAdd(
                path_in_repo=raw_info["path"], path_or_fileobj=str(raw_path)
            ))
        manifest = {
            "version": 3,
            "created_at": utcnow(),
            "files": len(files),
            "archive_bytes": state_info["bytes"] + sum(item["bytes"] for item in shards),
            "state": state_info,
            "raw_shards": shards,
            "raw_index": index,
            "raw_inventory_sha256": _raw_inventory(index),
        }
        operations.append(CommitOperationAdd(
            path_in_repo=f"{path_in_repo}/checkpoint.json",
            path_or_fileobj=json.dumps(manifest, indent=2).encode("utf-8"),
        ))
        commit = _commit_with_retry(
            api, repo=repo,
            operations=operations,
            message=f"Checkpoint {path_in_repo} ({len(files)} files)",
        )
    return {**manifest, "commit_url": str(commit.commit_url)}


def restore(repo: str, path_in_repo: str, *, token: str | None) -> dict:
    import zstandard
    from huggingface_hub import hf_hub_download

    DATA.mkdir(parents=True, exist_ok=True)
    path_in_repo = path_in_repo.strip("/")
    manifest = _existing_manifest(repo, path_in_repo, token)
    if manifest is None:
        try:
            hf_hub_download(repo_id=repo, filename=f"{path_in_repo}/{CHECKPOINT_NAME}",
                            repo_type="dataset", token=token)
        except Exception as error:
            if getattr(getattr(error, "response", None), "status_code", None) == 404:
                print(json.dumps({"restored": False, "reason": "checkpoint does not exist"}))
                return {"restored": False}
            raise
        raise RuntimeError("checkpoint archive exists without its manifest")

    if manifest.get("version") == 3:
        archive_infos = list(manifest.get("raw_shards") or []) + [manifest.get("state")]
        if not manifest.get("state"):
            raise RuntimeError("v3 checkpoint has no state archive")
        if manifest.get("raw_index") and not manifest.get("raw_shards"):
            raise RuntimeError("v3 checkpoint has a raw index but no shards")
    elif manifest.get("version") == 2:
        archive_infos = [item for item in (manifest.get("raw"), manifest.get("state")) if item]
        if not manifest.get("state"):
            raise RuntimeError("v2 checkpoint has no state archive")
    else:
        archive_infos = [{
            "path": manifest.get("path_in_repo") or f"{path_in_repo}/{CHECKPOINT_NAME}",
            "bytes": manifest.get("archive_bytes"),
            "sha256": manifest.get("archive_sha256"),
        }]
    downloaded = []
    for info in archive_infos:
        path = Path(hf_hub_download(
            repo_id=repo, filename=info["path"], repo_type="dataset", token=token,
        ))
        if info.get("bytes") != path.stat().st_size or info.get("sha256") != sha256_file(path):
            raise RuntimeError(f"checkpoint archive failed size or SHA-256 verification: {info['path']}")
        downloaded.append(path)
    for path in downloaded:
        with path.open("rb") as source:
            with zstandard.ZstdDecompressor().stream_reader(source) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    archive.extractall(PROJECT_ROOT, filter="data")
    if manifest.get("version") == 3:
        actual = _raw_index([
            (path, path.relative_to(PROJECT_ROOT).as_posix())
            for path in sorted((DATA / "raw").rglob("*")) if path.is_file()
        ]) if (DATA / "raw").is_dir() else {}
        if actual != manifest.get("raw_index"):
            raise RuntimeError("restored original-PDF inventory differs from v3 manifest")
    print(json.dumps({"restored": True, "archives": [str(path) for path in downloaded]}))
    return {"restored": True, "version": manifest.get("version", 1)}


def upload_dir(repo: str, path_in_repo: str, directory: Path, *, token: str | None) -> dict:
    from huggingface_hub import HfApi

    if not directory.is_dir():
        raise SystemExit(f"directory not found: {directory}")
    api = HfApi(token=token)
    api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
    commit = api.upload_folder(
        folder_path=str(directory),
        path_in_repo=path_in_repo.strip("/"),
        repo_id=repo,
        repo_type="dataset",
        ignore_patterns=["*.png", "*.jpg", "*.jpeg"],
        commit_message=f"Add {path_in_repo}",
    )
    return {"path_in_repo": path_in_repo, "commit_url": str(commit.commit_url)}


def upload(repo: str, path_in_repo: str, files: list[Path], *, token: str | None) -> dict:
    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
    path_in_repo = path_in_repo.strip("/")
    operations = [CommitOperationAdd(path_in_repo=f"{path_in_repo}/{path.name}",
                                     path_or_fileobj=str(path)) for path in files if path.is_file()]
    if not operations:
        return {"uploaded": []}
    commit = _commit_with_retry(api, repo=repo, operations=operations,
                                message=f"Add {len(operations)} run files to {path_in_repo}")
    return {"uploaded": [operation.path_in_repo for operation in operations],
            "commit_url": str(commit.commit_url)}


def upload_run(repo: str, path_in_repo: str, *, token: str | None) -> dict:
    from huggingface_hub import CommitOperationAdd, HfApi

    path_in_repo = path_in_repo.strip("/")
    files: list[tuple[Path, str]] = [(Path("/tmp/run-summary.json"), f"{path_in_repo}/run-summary.json")]
    for kind in ("complete", "skipped"):
        for path in Path("/tmp").glob(f"session-{kind}-*.json"):
            files.append((path, f"state/{kind}/{path.name}"))
    for path in Path("/tmp").glob("snapshot-complete-*.json"):
        files.append((path, f"state/snapshot-complete/{path.name}"))
    for path in Path("data/logs").glob("*.jsonl"):
        files.append((path, f"{path_in_repo}/{path.name}"))
    for path in Path("results/verification").rglob("*"):
        if path.is_file():
            files.append((path, f"{path_in_repo}/verification/{path.relative_to('results/verification').as_posix()}"))
    operations = [CommitOperationAdd(path_in_repo=remote, path_or_fileobj=str(local))
                  for local, remote in files if local.is_file()]
    api = HfApi(token=token)
    commit = _commit_with_retry(api, repo=repo, operations=operations,
                                message=f"Run state {path_in_repo}")
    return {"files": len(operations), "commit_url": str(commit.commit_url)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    save_parser = sub.add_parser("save", help="Tar and upload pipeline state")
    save_parser.add_argument("--repo", required=True)
    save_parser.add_argument("--path-in-repo", required=True)

    restore_parser = sub.add_parser("restore", help="Download and extract pipeline state")
    restore_parser.add_argument("--repo", required=True)
    restore_parser.add_argument("--path-in-repo", required=True)

    upload_parser = sub.add_parser("upload", help="Upload individual run files")
    upload_parser.add_argument("--repo", required=True)
    upload_parser.add_argument("--path-in-repo", required=True)
    upload_parser.add_argument("--file", type=Path, action="append", default=[])

    dir_parser = sub.add_parser(
        "upload-dir", help="Upload a directory tree, excluding rendered images"
    )
    dir_parser.add_argument("--repo", required=True)
    dir_parser.add_argument("--path-in-repo", required=True)
    dir_parser.add_argument("--dir", type=Path, required=True)

    run_parser = sub.add_parser("upload-run", help="Upload summary, markers and logs in one commit")
    run_parser.add_argument("--repo", required=True)
    run_parser.add_argument("--path-in-repo", required=True)

    args = parser.parse_args()
    import os

    token = os.environ.get("HF_TOKEN") or None
    if args.command == "save":
        print(json.dumps(save(args.repo, args.path_in_repo, token=token), indent=2))
    elif args.command == "restore":
        print(json.dumps(restore(args.repo, args.path_in_repo, token=token), indent=2))
    elif args.command == "upload-dir":
        print(json.dumps(upload_dir(args.repo, args.path_in_repo, args.dir, token=token), indent=2))
    elif args.command == "upload-run":
        print(json.dumps(upload_run(args.repo, args.path_in_repo, token=token), indent=2))
    else:
        if not args.file:
            raise SystemExit("--file is required at least once")
        print(json.dumps(upload(args.repo, args.path_in_repo, args.file, token=token), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
