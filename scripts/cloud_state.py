#!/usr/bin/env python3
"""Checkpoint and restore pipeline state to a Hugging Face dataset repository.

The GitHub Actions runner is ephemeral, so an interrupted session must be able
to resume without repeating model calls. The checkpoint contains the SQLite
state, the immutable raw PDFs, extraction artifacts and event logs. Rendered
page images are excluded because they are large and regenerable.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data"

EXCLUDE_NAMES = ("data/tmp", "data/exports", "data/publications")
EXCLUDE_SUFFIXES = (".png", ".jpg", ".jpeg")
CHECKPOINT_NAME = "checkpoint.tar.zst"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def save(repo: str, path_in_repo: str, *, token: str | None) -> dict:
    import zstandard
    from huggingface_hub import HfApi

    DATA.mkdir(parents=True, exist_ok=True)
    _checkpoint_sqlite(DATA / "pipeline.sqlite3")
    files = _iter_state_files()
    if not files:
        raise SystemExit("no pipeline state to checkpoint; run the pipeline first")
    path_in_repo = path_in_repo.strip("/")
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data") as temp_dir:
        archive_path = Path(temp_dir) / CHECKPOINT_NAME
        compressor = zstandard.ZstdCompressor(level=3)
        with archive_path.open("wb") as target:
            with compressor.stream_writer(target) as stream:
                with tarfile.open(fileobj=stream, mode="w|") as archive:
                    for path, relative in files:
                        archive.add(path, arcname=relative)
        size = archive_path.stat().st_size
        api = HfApi(token=token)
        api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
        commit = api.upload_file(
            path_or_fileobj=str(archive_path),
            path_in_repo=f"{path_in_repo}/{CHECKPOINT_NAME}",
            repo_id=repo,
            repo_type="dataset",
            commit_message=f"Checkpoint {path_in_repo} ({len(files)} files)",
        )
        manifest = {
            "created_at": utcnow(),
            "files": len(files),
            "archive_bytes": size,
            "path_in_repo": f"{path_in_repo}/{CHECKPOINT_NAME}",
        }
        api.upload_file(
            path_or_fileobj=json.dumps(manifest, indent=2).encode("utf-8"),
            path_in_repo=f"{path_in_repo}/checkpoint.json",
            repo_id=repo,
            repo_type="dataset",
            commit_message=f"Checkpoint manifest {path_in_repo}",
        )
    return {**manifest, "commit_url": str(commit.commit_url)}


def restore(repo: str, path_in_repo: str, *, token: str | None) -> dict:
    import zstandard
    from huggingface_hub import hf_hub_download

    DATA.mkdir(parents=True, exist_ok=True)
    path_in_repo = path_in_repo.strip("/")
    try:
        downloaded = hf_hub_download(
            repo_id=repo,
            filename=f"{path_in_repo}/{CHECKPOINT_NAME}",
            repo_type="dataset",
            token=token,
        )
    except Exception as error:
        print(json.dumps({"restored": False, "reason": str(error)[:300]}))
        return {"restored": False}
    with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "data") as temp_dir:
        archive_path = Path(temp_dir) / CHECKPOINT_NAME
        with Path(downloaded).open("rb") as source:
            with archive_path.open("wb") as target:
                target.write(source.read())
        with archive_path.open("rb") as source:
            decompressor = zstandard.ZstdDecompressor()
            with decompressor.stream_reader(source) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as archive:
                    archive.extractall(PROJECT_ROOT, filter="data")
    print(json.dumps({"restored": True, "archive": str(downloaded)}))
    return {"restored": True}


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
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
    path_in_repo = path_in_repo.strip("/")
    uploaded = []
    for path in files:
        if not path.is_file():
            continue
        commit = api.upload_file(
            path_or_fileobj=str(path),
            path_in_repo=f"{path_in_repo}/{path.name}",
            repo_id=repo,
            repo_type="dataset",
            commit_message=f"Add {path.name}",
        )
        uploaded.append({"file": str(path), "commit_url": str(commit.commit_url)})
    return {"uploaded": uploaded}


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

    args = parser.parse_args()
    import os

    token = os.environ.get("HF_TOKEN") or None
    if args.command == "save":
        print(json.dumps(save(args.repo, args.path_in_repo, token=token), indent=2))
    elif args.command == "restore":
        print(json.dumps(restore(args.repo, args.path_in_repo, token=token), indent=2))
    elif args.command == "upload-dir":
        print(json.dumps(upload_dir(args.repo, args.path_in_repo, args.dir, token=token), indent=2))
    else:
        if not args.file:
            raise SystemExit("--file is required at least once")
        print(json.dumps(upload(args.repo, args.path_in_repo, args.file, token=token), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
