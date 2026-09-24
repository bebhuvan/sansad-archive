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


def model_transcript_members(path: Path, scope: str) -> set[str]:
    match = SCOPE.fullmatch(scope)
    if not match:
        raise ValueError(f"invalid checkpoint scope: {scope}")
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        members = set()
        for (response_path,) in connection.execute(
            """WITH scoped AS (
                 SELECT DISTINCT document_sha256 FROM census_records
                 WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                   AND document_sha256 IS NOT NULL
               ), latest AS (
                 SELECT (SELECT MAX(r.id) FROM runs r
                         WHERE r.document_sha256=s.document_sha256
                           AND r.status='complete') run_id FROM scoped s
               )
               SELECT (SELECT a.response_path FROM adjudications a
                       WHERE a.run_id=p.run_id AND a.page_number=p.page_number
                         AND a.provider='openrouter'
                       ORDER BY a.id DESC LIMIT 1)
               FROM pages p JOIN latest x ON x.run_id=p.run_id""",
            match.groups(),
        ):
            if response_path is None:
                continue
            marker = "data/artifacts/"
            normalized = response_path.replace("\\", "/")
            if marker not in normalized:
                raise RuntimeError(f"model response path is outside checkpoint artifacts: {response_path}")
            relative = marker + normalized.split(marker, 1)[1]
            members.add(str(Path(relative).with_name("adjudicated.md")))
        return members
    finally:
        connection.close()


def audit_transcript_archive(state_archive: Path, expected: set[str]) -> dict:
    import zstandard

    found: set[str] = set()
    blank: set[str] = set()
    with state_archive.open("rb") as source:
        with zstandard.ZstdDecompressor().stream_reader(source) as stream:
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                for member in archive:
                    if member.name not in expected:
                        continue
                    found.add(member.name)
                    if not member.isfile() or member.size > 8 * 1024 * 1024:
                        blank.add(member.name)
                        continue
                    with archive.extractfile(member) as transcript:
                        payload = transcript.read()
                    try:
                        if not payload.decode("utf-8").strip():
                            blank.add(member.name)
                    except UnicodeDecodeError:
                        blank.add(member.name)
    return {
        "model_transcript_artifacts_expected": len(expected),
        "model_transcript_artifacts_missing": len(expected - found),
        "model_transcript_artifacts_blank_or_invalid": len(blank),
    }


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
        flags = {
            flag: count for flag, count in connection.execute(
                """WITH scoped AS (
                     SELECT DISTINCT document_sha256 FROM census_records
                     WHERE house=? AND COALESCE(parliament_number,'')=? AND session=?
                       AND document_sha256 IS NOT NULL
                   ), latest AS (
                     SELECT (SELECT MAX(r.id) FROM runs r
                             WHERE r.document_sha256=s.document_sha256
                               AND r.status='complete') run_id FROM scoped s
                   )
                   SELECT f.value,COUNT(*) FROM pages p
                   JOIN latest x ON x.run_id=p.run_id
                   JOIN json_each(p.flags_json) f
                   GROUP BY f.value""", params,
            )
        }
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
        attachment_summary = {"attachment_inventory_status": "not_applicable"}
        if scope.startswith("elibrary-"):
            table_present = connection.execute(
                """SELECT 1 FROM sqlite_master WHERE type='table'
                   AND name='elibrary_pdf_attachments'"""
            ).fetchone() is not None
            attachment_summary = {"attachment_inventory_status":
                                  "present" if table_present else "not_recorded"}
            if table_present:
                total = connection.execute(
                    """SELECT COUNT(*) FROM census_records
                       WHERE house=? AND COALESCE(parliament_number,'')=?
                         AND session=? AND record_id LIKE 'elibrary_%'
                         AND acquisition_status='downloaded'""", params,
                ).fetchone()[0]
                covered, pdfs, additional, distinct = connection.execute(
                    """SELECT COUNT(DISTINCT a.record_id),COUNT(*),
                              SUM(a.document_sha256<>c.document_sha256),
                              COUNT(DISTINCT a.document_sha256)
                       FROM elibrary_pdf_attachments a
                       JOIN census_records c ON c.record_id=a.record_id
                       WHERE c.house=? AND COALESCE(c.parliament_number,'')=?
                         AND c.session=? AND c.record_id LIKE 'elibrary_%'
                         AND c.acquisition_status='downloaded'""", params,
                ).fetchone()
                attachment_summary.update({
                    "attachment_items_total": int(total),
                    "attachment_items_inventoried": int(covered or 0),
                    "attachment_items_missing_inventory": int(total - (covered or 0)),
                    "attachment_pdf_bitstreams": int(pdfs or 0),
                    "attachment_additional_pdf_bitstreams": int(additional or 0),
                    "attachment_distinct_pdf_bytes": int(distinct or 0),
                })
        return {
            "source_records_by_acquisition_status": statuses,
            "distinct_acquired_pdfs": int(coverage[0] or 0),
            "processed_pdfs": int(coverage[1] or 0),
            "extracted_pages": int(coverage[2] or 0),
            "pages_by_route": by_route,
            "pages_by_validation_status": by_status,
            "validation_flag_counts": flags,
            "model_pages": int(coverage[3] or 0),
            "model_calls": int(cost[0] or 0),
            "cost_unknown_calls": int(cost[1] or 0),
            "cost_zero_calls": int(cost[2] or 0),
            "cost_nonzero_calls": int(cost[3] or 0),
            **attachment_summary,
        }
    finally:
        connection.close()


def inspect(repo: str, scope: str, *, max_db_bytes: int,
            max_state_bytes: int = 512 * 1024 * 1024,
            audit_transcripts: bool = False) -> dict:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import RemoteEntryNotFoundError

    if not SCOPE.fullmatch(scope):
        raise ValueError(f"invalid checkpoint scope: {scope}")
    root = f"state/checkpoints/{scope}"
    with tempfile.TemporaryDirectory(prefix="sansad-hf-monitor-") as directory:
        temporary = Path(directory)
        try:
            manifest_path = Path(hf_hub_download(
                repo, f"{root}/checkpoint.json", repo_type="dataset",
                local_dir=temporary, force_download=True,
            ))
        except RemoteEntryNotFoundError:
            return {"repo": repo, "scope": scope,
                    "checkpoint_status": "not_found", "checkpoint_at": None}
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
        summary = summarize_database(database_path, scope)
        transcript_audit = {}
        if audit_transcripts:
            expected = model_transcript_members(database_path, scope)
            if len(expected) != summary["model_pages"]:
                raise RuntimeError("model page count and transcript paths disagree")
            transcript_audit = audit_transcript_archive(state_path, expected)
        return {
            "repo": repo,
            "scope": scope,
            "checkpoint_status": "present",
            "checkpoint_at": manifest.get("created_at"),
            "checkpoint_version": manifest.get("version"),
            "retained_original_pdfs": raw_pdf_count(manifest),
            **summary,
            **transcript_audit,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--scope", action="append", required=True)
    parser.add_argument("--max-db-mib", type=int, default=1024)
    parser.add_argument("--max-state-mib", type=int, default=512)
    parser.add_argument("--audit-transcripts", action="store_true",
                        help="stream checkpoint state to verify saved model transcripts")
    args = parser.parse_args()
    if args.max_db_mib < 1 or args.max_state_mib < 1:
        parser.error("monitor disk limits must be positive")
    for scope in args.scope:
        print(json.dumps(inspect(args.repo, scope,
                                 max_db_bytes=args.max_db_mib * 1024 * 1024,
                                 max_state_bytes=args.max_state_mib * 1024 * 1024,
                                 audit_transcripts=args.audit_transcripts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
