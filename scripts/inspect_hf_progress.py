#!/usr/bin/env python3
"""Read HF checkpoint progress without retaining another local corpus cache.

Only the mutable state archive is downloaded. Original-PDF shards stay on HF;
their count comes from the checkpoint manifest, not from a local copy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import tarfile
import tempfile
from pathlib import Path


SCOPE = re.compile(r"^(?:elibrary-)?(lok_sabha|rajya_sabha)-p([0-9]*)-s([A-Za-z0-9_-]+)$")


def raw_pdf_count(manifest: dict) -> int:
    if manifest.get("version") == 3:
        return len(manifest.get("raw_index") or {})
    if manifest.get("version") == 2:
        return int((manifest.get("raw") or {}).get("files") or 0)
    raise ValueError("progress inspector requires a v2 or v3 checkpoint")


def verify_archive(path: Path, info: dict) -> None:
    if path.stat().st_size != info.get("bytes"):
        raise RuntimeError(f"state archive size mismatch: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != info.get("sha256"):
        raise RuntimeError(f"state archive SHA-256 mismatch: {path.name}")


def extract_database(state_archive: Path, target: Path, *, max_bytes: int) -> None:
    import zstandard

    with state_archive.open("rb") as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    if member.name != "data/pipeline.sqlite3":
                        continue
                    if not member.isfile() or member.size > max_bytes:
                        raise RuntimeError("checkpoint SQLite file exceeds monitor disk limit")
                    with archive.extractfile(member) as input_file, target.open("wb") as output:
                        shutil.copyfileobj(input_file, output, length=1024 * 1024)
                    return
    raise RuntimeError("checkpoint state archive has no SQLite database")


def summarize_database(path: Path, scope: str) -> dict:
    match = SCOPE.fullmatch(scope)
    if not match:
        raise ValueError(f"invalid checkpoint scope: {scope}")
    house, parliament, session = match.groups()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        params = (house, parliament, session)
        statuses = {
            status: count for status, count in connection.execute(
                """SELECT acquisition_status,COUNT(*) FROM census_records
                   WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                   GROUP BY acquisition_status""", params,
            )
        }
        coverage = connection.execute(
            """WITH scoped AS (
                 SELECT DISTINCT document_sha256 FROM census_records
                 WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                   AND document_sha256 IS NOT NULL
               ), latest AS (
                 SELECT s.document_sha256,
                        (SELECT MAX(r.id) FROM runs r
                         WHERE r.document_sha256=s.document_sha256
                           AND r.status='complete') run_id FROM scoped s
               )
               SELECT COUNT(*),SUM(l.run_id IS NOT NULL),
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id),
                      (SELECT COUNT(*) FROM pages p JOIN latest x ON x.run_id=p.run_id
                       WHERE EXISTS (SELECT 1 FROM adjudications a
                                     WHERE a.run_id=p.run_id AND a.page_number=p.page_number
                                       AND a.provider='openrouter'))
               FROM latest l""", params,
        ).fetchone()
        page_quality = connection.execute(
            """WITH scoped AS (
                 SELECT DISTINCT document_sha256 FROM census_records
                 WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                   AND document_sha256 IS NOT NULL
               ), latest AS (
                 SELECT (SELECT MAX(r.id) FROM runs r
                         WHERE r.document_sha256=s.document_sha256
                           AND r.status='complete') run_id FROM scoped s
               )
               SELECT p.route,p.validation_status,COUNT(*)
               FROM pages p JOIN latest x ON x.run_id=p.run_id
               GROUP BY p.route,p.validation_status""", params,
        ).fetchall()
        by_route: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for route, status, count in page_quality:
            by_route[route] = by_route.get(route, 0) + count
            by_status[status] = by_status.get(status, 0) + count
        cost = connection.execute(
            """SELECT COUNT(*),SUM(a.reported_cost IS NULL),SUM(a.reported_cost=0),
                      SUM(a.reported_cost!=0)
               FROM adjudications a JOIN runs r ON r.id=a.run_id
               WHERE a.provider='openrouter'
                 AND EXISTS (SELECT 1 FROM census_records c
                             WHERE c.document_sha256=r.document_sha256
                               AND c.house=?
                               AND COALESCE(c.parliament_number,'')=?
                               AND c.session=?)""", params,
        ).fetchone()
        return {
            "source_records_by_acquisition_status": statuses,
            "distinct_acquired_pdfs": int(coverage[0] or 0),
            "processed_pdfs": int(coverage[1] or 0),
            "extracted_pages": int(coverage[2] or 0),
            "pages_by_route": by_route,
            "pages_by_validation_status": by_status,
            "model_pages": int(coverage[3] or 0),
            "model_calls": int(cost[0] or 0),
            "cost_unknown_calls": int(cost[1] or 0),
            "cost_zero_calls": int(cost[2] or 0),
            "cost_nonzero_calls": int(cost[3] or 0),
        }
    finally:
        connection.close()


def inspect(repo: str, scope: str, *, max_db_bytes: int,
            max_state_bytes: int = 512 * 1024 * 1024) -> dict:
    from huggingface_hub import hf_hub_download

    if not SCOPE.fullmatch(scope):
        raise ValueError(f"invalid checkpoint scope: {scope}")
    root = f"state/checkpoints/{scope}"
    with tempfile.TemporaryDirectory(prefix="sansad-hf-monitor-") as directory:
        temporary = Path(directory)
        manifest_path = Path(hf_hub_download(
            repo, f"{root}/checkpoint.json", repo_type="dataset",
            local_dir=temporary, force_download=True,
        ))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        state = manifest.get("state") or {}
        state_name = state.get("path")
        if not isinstance(state_name, str) or state_name != f"{root}/state.tar.zst":
            raise RuntimeError("checkpoint state path does not match requested scope")
        if not isinstance(state.get("bytes"), int) or state["bytes"] > max_state_bytes:
            raise RuntimeError("checkpoint state archive exceeds monitor disk limit")
        state_path = Path(hf_hub_download(
            repo, state_name, repo_type="dataset", local_dir=temporary,
            force_download=True,
        ))
        verify_archive(state_path, state)
        database_path = temporary / "monitor.sqlite3"
        extract_database(state_path, database_path, max_bytes=max_db_bytes)
        return {
            "repo": repo,
            "scope": scope,
            "checkpoint_at": manifest.get("created_at"),
            "checkpoint_version": manifest.get("version"),
            "retained_original_pdfs": raw_pdf_count(manifest),
            **summarize_database(database_path, scope),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--max-db-mib", type=int, default=1024)
    parser.add_argument("--max-state-mib", type=int, default=512)
    args = parser.parse_args()
    if args.max_db_mib < 1 or args.max_state_mib < 1:
        parser.error("monitor disk limits must be positive")
    for scope in args.scope:
        print(json.dumps(inspect(args.repo, scope,
                                 max_db_bytes=args.max_db_mib * 1024 * 1024,
                                 max_state_bytes=args.max_state_mib * 1024 * 1024)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
