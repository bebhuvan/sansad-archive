from __future__ import annotations

import gzip
import hashlib
import json
import math
import shutil
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import json_text
from .sources.questions import (
    QuestionRecord,
    available_lok_sabha_sessions,
    available_rajya_sabha_sessions,
    discover_lok_sabha_questions,
    discover_rajya_sabha_questions,
    latest_lok_sabha_session,
    latest_rajya_sabha_session,
)
from .sources.elibrary import (
    lok_sabha_question_count as elibrary_question_count,
    records_from_search_response,
    list_original_pdfs,
    resolve_original_pdf,
    search_page as elibrary_search_page,
    normalize_session_label,
    classify_language,
)
from .storage import Store


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Census:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config)
        self.store.initialize()

    def _record_elibrary_pdf_attachments(
        self, record_id: str, pdfs: list[tuple[str, dict]], digests: list[str],
        *, primary_sha256: str | None = None,
    ) -> None:
        if not pdfs or len(pdfs) != len(digests):
            raise RuntimeError(f"eLibrary PDF inventory/download count mismatch for {record_id}")
        if primary_sha256 and primary_sha256 not in digests:
            raise RuntimeError(
                f"eLibrary original PDF no longer matches selected PDF for {record_id}"
            )
        with self.store.db.connect() as connection:
            for position, ((url, bitstream), digest) in enumerate(zip(pdfs, digests)):
                bitstream_id = str(bitstream.get("uuid") or bitstream.get("id"))
                existing = connection.execute(
                    """SELECT document_sha256 FROM elibrary_pdf_attachments
                       WHERE record_id=? AND bitstream_id=?""",
                    (record_id, bitstream_id),
                ).fetchone()
                if existing and existing["document_sha256"] != digest:
                    raise RuntimeError(
                        f"eLibrary bitstream changed for {record_id} {bitstream_id}; "
                        "refusing to replace archived bytes"
                    )
                connection.execute(
                    """INSERT OR IGNORE INTO elibrary_pdf_attachments
                       (record_id,bitstream_id,position,name,source_url,
                        document_sha256,acquired_at)
                       VALUES (?,?,?,?,?,?,?)""",
                    (record_id, bitstream_id, position,
                     str(bitstream.get("name") or ""), url, digest, utcnow()),
                )

    def _upsert_many(self, records: list[QuestionRecord]) -> None:
        if not records:
            return
        statement = """INSERT INTO census_records
               (record_id,source_type,house,parliament_number,session,document_number,
                document_subtype,document_date,title,ministry,members_json,language,
                source_url,official_page_url,api_url,api_params_json,raw_json,discovered_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(record_id) DO UPDATE SET
                 source_type=excluded.source_type, house=excluded.house,
                 parliament_number=excluded.parliament_number, session=excluded.session,
                 document_number=excluded.document_number,
                 document_subtype=excluded.document_subtype,
                 document_date=excluded.document_date,
                 title=excluded.title, ministry=excluded.ministry,
                 language=excluded.language,
                 members_json=excluded.members_json, source_url=excluded.source_url,
                 official_page_url=excluded.official_page_url, api_url=excluded.api_url,
                 api_params_json=excluded.api_params_json, raw_json=excluded.raw_json,
                 discovered_at=excluded.discovered_at"""
        discovered_at = utcnow()
        values = [
            (
                record.record_id, record.source_type, record.house, record.parliament_number,
                record.session, record.document_number, record.document_subtype,
                record.document_date, record.title, record.ministry, json_text(record.members),
                record.language, record.source_url, record.official_page_url, record.api_url,
                json_text(record.api_params), json_text(record.raw), discovered_at,
            )
            for record in records
        ]
        with self.store.db.connect() as connection:
            connection.executemany(statement, values)

    def import_snapshot(
        self, path: Path, *, source: str = "all", house: str | None = None,
        parliament: str | None = None, session: str | None = None,
        offset: int = 0, limit: int = 0, expected_sha256: str | None = None,
    ) -> dict:
        """Import a bounded, idempotent slice of a verified census JSONL export."""
        if source not in {"all", "current", "elibrary"} or offset < 0 or limit < 0:
            raise ValueError("source must be all/current/elibrary; offset and limit must be non-negative")
        if not path.is_file():
            raise FileNotFoundError(path)
        if expected_sha256:
            digest = hashlib.sha256()
            with path.open("rb") as source_file:
                for block in iter(lambda: source_file.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise RuntimeError("census snapshot SHA-256 mismatch; refusing import")
        open_text = gzip.open if path.suffix == ".gz" else open
        fields = tuple(QuestionRecord.__dataclass_fields__)
        seen: set[str] = set()
        matched = imported = 0
        batch: list[QuestionRecord] = []
        with open_text(path, "rt", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    row = json.loads(line)
                    record_id = str(row["record_id"])
                    if not record_id:
                        raise ValueError("empty record ID")
                    if source == "elibrary" and not record_id.startswith("elibrary_"):
                        continue
                    if source == "current" and record_id.startswith("elibrary_"):
                        continue
                    if record_id.startswith("elibrary_"):
                        normalized, repair = normalize_session_label(
                            str(row.get("session") or ""), row.get("members") or []
                        )
                        if repair:
                            row["session"] = normalized
                            row["raw"] = {**(row.get("raw") or {}), "session_normalization": repair}
                        row["language"] = classify_language(
                            (row.get("raw") or {}).get("dc.language.iso") or []
                        )
                    if house is not None and row["house"] != house:
                        continue
                    if parliament is not None and str(row["parliament_number"]) != parliament:
                        continue
                    if session is not None and str(row["session"]) != session:
                        continue
                    matched += 1
                    if matched <= offset:
                        continue
                    if record_id in seen:
                        raise ValueError(f"duplicate selected record ID: {record_id}")
                    seen.add(record_id)
                    batch.append(QuestionRecord(**{field: row[field] for field in fields}))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RuntimeError(f"invalid census snapshot at line {line_number}: {error}") from error
                imported += 1
                if len(batch) >= 100:
                    self._upsert_many(batch)
                    batch.clear()
                if limit and imported >= limit:
                    break
        if batch:
            self._upsert_many(batch)
        if (source == "elibrary" and house == "lok_sabha" and parliament is not None
                and session is not None and offset == 0 and limit == 0):
            finished = utcnow()
            run_id = self.store.db.execute(
                """INSERT INTO census_runs
                   (source_type,scope_json,status,started_at,finished_at,records_seen)
                   VALUES ('questions_answers_elibrary_snapshot',?,'complete',?,?,?)""",
                (json_text({"snapshot": str(path), "sha256": expected_sha256,
                            "house": house, "parliament": parliament,
                            "session": session}), finished, finished, imported),
            )
            self.store.db.execute(
                """INSERT INTO census_scopes
                   (run_id,house,parliament_number,session,status,records_seen,started_at,finished_at)
                   VALUES (?,?,?,?,'complete',?,?,?)""",
                (run_id, house, parliament, session, imported, finished, finished),
            )
        return {"imported": imported, "matched_before_limit": matched,
                "offset": offset, "limit": limit, "source": source,
                "house": house, "parliament": parliament, "session": session}

    def discover_lok_sabha(
        self,
        *,
        lok_sabha: str | None,
        session: str | None,
        all_available: bool,
        limit: int,
        page_size: int,
        sleep_seconds: float,
        workers: int,
        retry_failed_run: int | None = None,
    ) -> dict:
        if retry_failed_run is not None:
            rows = self.store.db.all(
                """SELECT parliament_number,session FROM census_scopes
                   WHERE run_id=? AND status='failed'
                   ORDER BY CAST(parliament_number AS INTEGER),CAST(session AS INTEGER)""",
                (retry_failed_run,),
            )
            scopes = [(str(row["parliament_number"]), str(row["session"])) for row in rows]
            if not scopes:
                raise ValueError(f"census run {retry_failed_run} has no failed scopes")
        elif all_available:
            scopes = available_lok_sabha_sessions()
        elif lok_sabha is None and session is None:
            scopes = [latest_lok_sabha_session()]
        elif lok_sabha is not None and session is not None:
            scopes = [(lok_sabha, session)]
        else:
            raise ValueError("provide both --lok-sabha and --session, or neither for latest")
        scope = {
            "house": "lok_sabha", "scopes": scopes, "all_available": all_available,
            "limit_per_session": limit, "page_size": page_size,
            "workers": workers,
            "retry_failed_run": retry_failed_run,
        }
        run_id = self.store.db.execute(
            """INSERT INTO census_runs(source_type,scope_json,status,started_at)
               VALUES ('questions_answers',?,'running',?)""",
            (json_text(scope), utcnow()),
        )
        seen = 0
        failed_scopes: list[dict] = []
        executor: ThreadPoolExecutor | None = None
        futures = {}
        try:
            for house_number, session_number in scopes:
                self.store.db.execute(
                    """INSERT INTO census_scopes
                       (run_id,house,parliament_number,session,status,started_at)
                       VALUES (?,'lok_sabha',?,?,'pending',?)""",
                    (run_id, house_number, session_number, utcnow()),
                )

            def fetch(scope: tuple[str, str]) -> tuple[tuple[str, str], list[QuestionRecord]]:
                house_number, session_number = scope
                self.store.db.execute(
                    """UPDATE census_scopes SET status='running',started_at=?
                       WHERE run_id=? AND house='lok_sabha' AND parliament_number=? AND session=?""",
                    (utcnow(), run_id, house_number, session_number),
                )
                records = list(
                    discover_lok_sabha_questions(
                        house_number, session_number, limit=limit, page_size=page_size,
                        sleep_seconds=sleep_seconds,
                    )
                )
                return scope, records

            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(fetch, item): item for item in scopes}
            for future in as_completed(futures):
                house_number, session_number = futures[future]
                try:
                    _, records = future.result()
                    for offset in range(0, len(records), 100):
                        self._upsert_many(records[offset : offset + 100])
                    seen += len(records)
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='complete',records_seen=?,finished_at=?
                           WHERE run_id=? AND house='lok_sabha' AND parliament_number=? AND session=?""",
                        (len(records), utcnow(), run_id, house_number, session_number),
                    )
                    print(
                        f"census lok_sabha={house_number} session={session_number} "
                        f"records={len(records)} total_seen={seen}", flush=True,
                    )
                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    failed_scopes.append(
                        {"lok_sabha": house_number, "session": session_number, "error": detail}
                    )
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='failed',finished_at=?,error=?
                           WHERE run_id=? AND house='lok_sabha' AND parliament_number=? AND session=?""",
                        (utcnow(), detail, run_id, house_number, session_number),
                    )
            executor.shutdown(wait=True)
            executor = None
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=? WHERE id=?""",
                ("partial" if failed_scopes else "complete", seen, utcnow(), run_id),
            )
        except BaseException as error:
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            for future in futures:
                future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            self.store.db.execute(
                """UPDATE census_scopes SET status=?,finished_at=?
                   WHERE run_id=? AND status IN ('pending','running')""",
                (status, utcnow(), run_id),
            )
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=?,error=? WHERE id=?""",
                (status, seen, utcnow(), f"{type(error).__name__}: {error}", run_id),
            )
            raise
        return {
            "run_id": run_id, "records_seen": seen, "scopes": scopes,
            "failed_scopes": failed_scopes,
        }

    def discover_rajya_sabha(
        self, *, session: str | None, all_available: bool, limit: int, workers: int
    ) -> dict:
        sessions = (
            available_rajya_sabha_sessions()
            if all_available
            else [session or latest_rajya_sabha_session()]
        )
        scope = {
            "house": "rajya_sabha",
            "sessions": sessions,
            "all_available": all_available,
            "limit_per_session": limit,
            "workers": workers,
        }
        run_id = self.store.db.execute(
            """INSERT INTO census_runs(source_type,scope_json,status,started_at)
               VALUES ('questions_answers_rs',?,'running',?)""",
            (json_text(scope), utcnow()),
        )
        seen = 0
        failed_scopes: list[dict] = []
        executor: ThreadPoolExecutor | None = None
        futures = {}
        try:
            started_at = utcnow()
            with self.store.db.connect() as connection:
                connection.executemany(
                    """INSERT INTO census_scopes
                       (run_id,house,parliament_number,session,status,started_at)
                       VALUES (?,'rajya_sabha','council',?,'pending',?)""",
                    [(run_id, item, started_at) for item in sessions],
                )

            def fetch(item: str) -> tuple[str, list[QuestionRecord]]:
                self.store.db.execute(
                    """UPDATE census_scopes SET status='running',started_at=?
                       WHERE run_id=? AND house='rajya_sabha' AND session=?""",
                    (utcnow(), run_id, item),
                )
                return item, list(discover_rajya_sabha_questions(item, limit=limit))

            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(fetch, item): item for item in sessions}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    _, records = future.result()
                    for offset in range(0, len(records), 100):
                        self._upsert_many(records[offset : offset + 100])
                    seen += len(records)
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='complete',records_seen=?,finished_at=?
                           WHERE run_id=? AND house='rajya_sabha' AND session=?""",
                        (len(records), utcnow(), run_id, item),
                    )
                    print(
                        f"census rajya_sabha session={item} records={len(records)} "
                        f"total_seen={seen}",
                        flush=True,
                    )
                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    failed_scopes.append({"session": item, "error": detail})
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='failed',finished_at=?,error=?
                           WHERE run_id=? AND house='rajya_sabha' AND session=?""",
                        (utcnow(), detail, run_id, item),
                    )
            executor.shutdown(wait=True)
            executor = None
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=? WHERE id=?""",
                ("partial" if failed_scopes else "complete", seen, utcnow(), run_id),
            )
        except BaseException as error:
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            for future in futures:
                future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            self.store.db.execute(
                """UPDATE census_scopes SET status=?,finished_at=?
                   WHERE run_id=? AND status IN ('pending','running')""",
                (status, utcnow(), run_id),
            )
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=?,error=? WHERE id=?""",
                (status, seen, utcnow(), f"{type(error).__name__}: {error}", run_id),
            )
            raise
        return {
            "run_id": run_id,
            "records_seen": seen,
            "sessions": sessions,
            "failed_scopes": failed_scopes,
        }

    def discover_elibrary_lok_sabha(
        self,
        *,
        limit: int,
        page_size: int,
        start_page: int,
        workers: int,
        retry_failed_run: int | None = None,
    ) -> dict:
        total_available = elibrary_question_count()
        if retry_failed_run is not None:
            rows = self.store.db.all(
                """SELECT session FROM census_scopes
                   WHERE run_id=? AND house='lok_sabha_elibrary'
                     AND status IN ('failed','interrupted')
                   ORDER BY CAST(session AS INTEGER)""",
                (retry_failed_run,),
            )
            pages = [int(row["session"]) for row in rows]
            if not pages:
                raise ValueError(f"census run {retry_failed_run} has no failed eLibrary pages")
        else:
            available_from_start = max(0, total_available - start_page * page_size)
            wanted = min(limit, available_from_start) if limit else available_from_start
            pages = list(range(start_page, start_page + math.ceil(wanted / page_size)))
        scope = {
            "house": "lok_sabha",
            "source": "sansad_elibrary_dspace",
            "collection_items": total_available,
            "limit": limit,
            "page_size": page_size,
            "start_page": start_page,
            "workers": workers,
            "retry_failed_run": retry_failed_run,
            "pages": len(pages),
        }
        run_id = self.store.db.execute(
            """INSERT INTO census_runs(source_type,scope_json,status,started_at)
               VALUES ('questions_answers_elibrary',?,'running',?)""",
            (json_text(scope), utcnow()),
        )
        seen = 0
        failed_pages: list[dict] = []
        executor: ThreadPoolExecutor | None = None
        futures = {}
        try:
            started_at = utcnow()
            with self.store.db.connect() as connection:
                connection.executemany(
                    """INSERT INTO census_scopes
                       (run_id,house,parliament_number,session,status,started_at)
                       VALUES (?,'lok_sabha_elibrary','all',?,'pending',?)""",
                    [(run_id, str(page), started_at) for page in pages],
                )

            def fetch(page: int) -> tuple[int, list[QuestionRecord]]:
                self.store.db.execute(
                    """UPDATE census_scopes SET status='running',started_at=?
                       WHERE run_id=? AND house='lok_sabha_elibrary' AND session=?""",
                    (utcnow(), run_id, str(page)),
                )
                response = elibrary_search_page(page=page, page_size=page_size)
                return page, records_from_search_response(response, page=page)

            executor = ThreadPoolExecutor(max_workers=workers)
            futures = {executor.submit(fetch, page): page for page in pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    _, records = future.result()
                    if limit and retry_failed_run is None:
                        page_offset = (page - start_page) * page_size
                        records = records[: max(0, min(page_size, limit - page_offset))]
                    for offset in range(0, len(records), 100):
                        self._upsert_many(records[offset : offset + 100])
                    seen += len(records)
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='complete',records_seen=?,finished_at=?
                           WHERE run_id=? AND house='lok_sabha_elibrary' AND session=?""",
                        (len(records), utcnow(), run_id, str(page)),
                    )
                    self.store.db.execute(
                        "UPDATE census_runs SET records_seen=? WHERE id=?", (seen, run_id)
                    )
                    if seen % 1000 == 0 or len(records) < page_size:
                        print(
                            f"census elibrary page={page} records={len(records)} "
                            f"total_seen={seen} available={total_available}",
                            flush=True,
                        )
                except Exception as error:
                    detail = f"{type(error).__name__}: {error}"
                    failed_pages.append({"page": page, "error": detail})
                    self.store.db.execute(
                        """UPDATE census_scopes SET status='failed',finished_at=?,error=?
                           WHERE run_id=? AND house='lok_sabha_elibrary' AND session=?""",
                        (utcnow(), detail, run_id, str(page)),
                    )
            executor.shutdown(wait=True)
            executor = None
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=? WHERE id=?""",
                ("partial" if failed_pages else "complete", seen, utcnow(), run_id),
            )
        except BaseException as error:
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            for future in futures:
                future.cancel()
            if executor is not None:
                executor.shutdown(wait=False, cancel_futures=True)
            self.store.db.execute(
                """UPDATE census_scopes SET status=?,finished_at=?
                   WHERE run_id=? AND status IN ('pending','running')""",
                (status, utcnow(), run_id),
            )
            self.store.db.execute(
                """UPDATE census_runs SET status=?,records_seen=?,finished_at=?,error=? WHERE id=?""",
                (status, seen, utcnow(), f"{type(error).__name__}: {error}", run_id),
            )
            raise
        return {
            "run_id": run_id,
            "records_seen": seen,
            "collection_items": total_available,
            "start_page": start_page,
            "failed_pages": failed_pages,
        }

    def acquire_questions(
        self,
        *,
        limit: int = 0,
        house: str | None = None,
        lok_sabha: str | None = None,
        session: str | None = None,
        source: str = "all",
        workers: int = 2,
        retry_failed: bool = False,
        min_free_gib: float = 10.0,
    ) -> dict:
        desired_status = "failed" if retry_failed else "discovered"
        where = ["source_type='questions_answers'", "acquisition_status=?"]
        base_params: list[object] = [desired_status]
        if house:
            where.append("house=?")
            base_params.append(house)
        if lok_sabha:
            where.append("parliament_number=?")
            base_params.append(lok_sabha)
        if session:
            where.append("session=?")
            base_params.append(session)
        if source == "elibrary":
            where.append("record_id LIKE 'elibrary_%'")
        elif source == "current":
            where.append("record_id NOT LIKE 'elibrary_%'")
        elif source != "all":
            raise ValueError("source must be all, current, or elibrary")

        downloaded = failed = selected = bytes_added = original_pdfs_downloaded = 0
        stopped_low_disk = False
        minimum_free = round(min_free_gib * 1024**3)
        last_retry_id = ""

        def acquire(row) -> tuple[str, object]:
            try:
                source_url = row["source_url"]
                raw = json.loads(row["raw_json"])
                extra_metadata = {}
                pdfs = []
                if raw.get("_source_system") == "sansad_elibrary_dspace":
                    item_id = str(raw.get("uuid") or raw.get("id") or "")
                    pdfs = list_original_pdfs(item_id)
                    source_url, bitstream = pdfs[0]
                    extra_metadata["elibrary_bitstream"] = bitstream
                    extra_metadata["elibrary_primary_pdf"] = True
                    extra_metadata["elibrary_original_pdf_inventory"] = [
                        {
                            "uuid": str(pdf.get("uuid") or pdf.get("id")),
                            "name": pdf.get("name"),
                            "size_bytes": pdf.get("sizeBytes"),
                            "content_url": url,
                        }
                        for url, pdf in pdfs
                    ]
                source_metadata = {
                    key: row[key] for key in row.keys() if key not in {"raw_json"}
                }
                document = self.store.download(
                    source_url,
                    metadata={
                        **source_metadata,
                        **extra_metadata,
                    },
                )
                documents = [document]
                for attachment_url, attachment in pdfs[1:]:
                    documents.append(self.store.download(
                        attachment_url,
                        metadata={
                            **source_metadata,
                            "elibrary_bitstream": attachment,
                            "elibrary_original_pdf_inventory": extra_metadata[
                                "elibrary_original_pdf_inventory"
                            ],
                            "elibrary_primary_pdf": False,
                        },
                    ))
                return "downloaded", (document, documents, pdfs)
            except Exception as error:
                return "failed", error

        batch_size = max(20, workers * 10)
        while not limit or selected < limit:
            if shutil.disk_usage(self.config.data_root).free < minimum_free:
                stopped_low_disk = True
                break
            count = min(batch_size, limit - selected) if limit else batch_size
            query_where = list(where)
            query_params = list(base_params)
            if retry_failed:
                query_where.append("record_id>?")
                query_params.append(last_retry_id)
            rows = self.store.db.all(
                f"""SELECT * FROM census_records WHERE {' AND '.join(query_where)}
                    ORDER BY {'record_id' if retry_failed else 'document_date,record_id'} LIMIT ?""",
                tuple([*query_params, count]),
            )
            if not rows:
                break
            if retry_failed:
                last_retry_id = rows[-1]["record_id"]
            selected += len(rows)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {executor.submit(acquire, row): row for row in rows}
                for future in as_completed(futures):
                    row = futures[future]
                    status, value = future.result()
                    if status == "downloaded":
                        document, documents, pdfs = value
                        if pdfs:
                            self._record_elibrary_pdf_attachments(
                                row["record_id"], pdfs, [item.sha256 for item in documents],
                                primary_sha256=document.sha256,
                            )
                        self.store.db.execute(
                            """UPDATE census_records SET acquisition_status='downloaded',
                               document_sha256=?,last_error=NULL WHERE record_id=?""",
                            (document.sha256, row["record_id"]),
                        )
                        downloaded += 1
                        original_pdfs_downloaded += len(documents)
                        bytes_added += sum(
                            item.size_bytes for item in documents if not item.already_present
                        )
                        print(
                            f"[{downloaded + failed}/{selected}] downloaded "
                            f"{row['record_id']} {document.sha256[:12]} "
                            f"original_pdfs={len(documents)}",
                            flush=True,
                        )
                    else:
                        error = value
                        failed += 1
                        self.store.db.execute(
                            """UPDATE census_records SET acquisition_status='failed',last_error=?
                               WHERE record_id=?""",
                            (f"{type(error).__name__}: {error}", row["record_id"]),
                        )
                        print(
                            f"[{downloaded + failed}/{selected}] failed {row['record_id']}: {error}",
                            flush=True,
                        )
        return {
            "selected": selected,
            "downloaded": downloaded,
            "original_pdfs_downloaded": original_pdfs_downloaded,
            "failed": failed,
            "bytes_added": bytes_added,
            "stopped_low_disk": stopped_low_disk,
            "minimum_free_gib": min_free_gib,
        }

    def backfill_elibrary_pdf_attachments(
        self, *, house: str, parliament: str, session: str,
        limit: int = 100, after_record_id: str = "", workers: int = 4,
        min_free_gib: float = 2.0,
    ) -> dict:
        """Archive all ORIGINAL PDFs for previously acquired eLibrary items.

        The selected PDF is reused only when its recorded source URI matches
        the current bitstream URL. An item gets a complete attachment ledger
        only after every PDF has been acquired and one matches its selected SHA.
        """
        if limit < 1 or workers < 1:
            raise ValueError("backfill limit and workers must be positive")
        rows = self.store.db.all(
            """SELECT c.* FROM census_records c
               WHERE c.house=? AND COALESCE(c.parliament_number,'')=?
                 AND c.session=? AND c.record_id LIKE 'elibrary_%'
                 AND c.acquisition_status='downloaded' AND c.document_sha256 IS NOT NULL
                 AND c.record_id>?
                 AND NOT EXISTS (SELECT 1 FROM elibrary_pdf_attachments a
                                 WHERE a.record_id=c.record_id)
               ORDER BY c.record_id LIMIT ?""",
            (house, parliament, session, after_record_id, limit),
        )
        minimum_free = round(min_free_gib * 1024**3)
        if shutil.disk_usage(self.config.data_root).free < minimum_free:
            return {"selected": 0, "completed": 0, "failed": 0,
                    "original_pdfs": 0, "new_original_pdfs": 0, "bytes_added": 0,
                    "stopped_low_disk": True, "last_record_id": after_record_id}

        def archive(row):
            item_id = str(json.loads(row["raw_json"]).get("uuid") or "")
            pdfs = list_original_pdfs(item_id)
            known = {
                source["source_uri"] for source in self.store.db.all(
                    "SELECT source_uri FROM sources WHERE document_sha256=?",
                    (row["document_sha256"],),
                )
            }
            inventory = [
                {"uuid": str(pdf.get("uuid") or pdf.get("id")),
                 "name": pdf.get("name"), "size_bytes": pdf.get("sizeBytes"),
                 "content_url": url}
                for url, pdf in pdfs
            ]
            digests = []
            new_pdfs = bytes_added = 0
            source_metadata = {key: row[key] for key in row.keys() if key != "raw_json"}
            for url, pdf in pdfs:
                if url in known:
                    digests.append(row["document_sha256"])
                    continue
                document = self.store.download(
                    url,
                    metadata={**source_metadata, "elibrary_bitstream": pdf,
                              "elibrary_original_pdf_inventory": inventory},
                )
                digests.append(document.sha256)
                new_pdfs += 1
                if not document.already_present:
                    bytes_added += document.size_bytes
            return pdfs, digests, new_pdfs, bytes_added

        completed = failed = original_pdfs = new_original_pdfs = bytes_added = 0
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(archive, row): row for row in rows}
            for future in as_completed(futures):
                row = futures[future]
                try:
                    pdfs, digests, new_pdfs, added = future.result()
                    self._record_elibrary_pdf_attachments(
                        row["record_id"], pdfs, digests,
                        primary_sha256=row["document_sha256"],
                    )
                except Exception as error:
                    failed += 1
                    self.store.db.execute(
                        "UPDATE census_records SET last_error=? WHERE record_id=?",
                        (f"attachment backfill: {type(error).__name__}: {error}",
                         row["record_id"]),
                    )
                    print(f"attachment backfill failed {row['record_id']}: {error}", flush=True)
                    continue
                completed += 1
                original_pdfs += len(pdfs)
                new_original_pdfs += new_pdfs
                bytes_added += added
                self.store.db.execute(
                    "UPDATE census_records SET last_error=NULL WHERE record_id=?",
                    (row["record_id"],),
                )
                print(f"attachment backfill {row['record_id']} original_pdfs={len(pdfs)}",
                      flush=True)
        return {"selected": len(rows), "completed": completed, "failed": failed,
                "original_pdfs": original_pdfs,
                "new_original_pdfs": new_original_pdfs, "bytes_added": bytes_added,
                "stopped_low_disk": False,
                "last_record_id": rows[-1]["record_id"] if rows else after_record_id}

    def estimate_elibrary_storage(self, *, samples: int, workers: int) -> dict:
        total = elibrary_question_count()
        page_size = 100
        total_pages = math.ceil(total / page_size)
        if samples == 1:
            sample_pages = [total_pages // 2]
        else:
            sample_pages = sorted(
                {
                    round(index * (total_pages - 1) / (samples - 1))
                    for index in range(samples)
                }
            )

        def measure(page: int) -> dict:
            response = elibrary_search_page(page=page, page_size=page_size)
            records = records_from_search_response(response, page=page)
            if not records:
                raise RuntimeError(f"eLibrary sample page {page} returned no records")
            item_id = str(records[len(records) // 2].raw["uuid"])
            _, bitstream = resolve_original_pdf(item_id)
            return {
                "page": page,
                "item_id": item_id,
                "name": bitstream.get("name"),
                "size_bytes": int(bitstream.get("sizeBytes") or 0),
            }

        measurements: list[dict] = []
        failures: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(measure, page): page for page in sample_pages}
            for future in as_completed(futures):
                page = futures[future]
                try:
                    measurements.append(future.result())
                except Exception as error:
                    failures.append({"page": page, "error": f"{type(error).__name__}: {error}"})
        sizes = sorted(row["size_bytes"] for row in measurements if row["size_bytes"] > 0)
        if not sizes:
            raise RuntimeError("no usable eLibrary bitstream sizes were sampled")
        mean = statistics.fmean(sizes)
        median = statistics.median(sizes)
        projected = round(mean * total)
        disk = shutil.disk_usage(self.config.data_root)
        return {
            "collection_items": total,
            "requested_samples": samples,
            "successful_samples": len(sizes),
            "failed_samples": failures,
            "sample_min_bytes": min(sizes),
            "sample_median_bytes": round(median),
            "sample_mean_bytes": round(mean),
            "sample_max_bytes": max(sizes),
            "projected_raw_bytes": projected,
            "projected_raw_gib": projected / 1024**3,
            "local_free_bytes": disk.free,
            "local_free_gib": disk.free / 1024**3,
            "fits_current_disk_by_mean": projected <= disk.free,
            "measurements": sorted(measurements, key=lambda row: row["page"]),
        }

    def status(self) -> dict:
        rows = self.store.db.all(
            """SELECT source_type,house,acquisition_status,COUNT(*) AS records
               FROM census_records GROUP BY source_type,house,acquisition_status
               ORDER BY source_type,house,acquisition_status"""
        )
        totals = self.store.db.one(
            """SELECT COUNT(*) AS records,
                      COUNT(DISTINCT source_url) AS unique_source_urls,
                      SUM(CASE WHEN record_id LIKE 'elibrary_%' THEN 1 ELSE 0 END)
                        AS elibrary_records,
                      SUM(CASE WHEN record_id NOT LIKE 'elibrary_%' THEN 1 ELSE 0 END)
                        AS current_api_records
               FROM census_records"""
        )
        runs = self.store.db.all(
            """SELECT id,source_type,status,records_seen,started_at,finished_at,error
               FROM census_runs ORDER BY id DESC LIMIT 10"""
        )
        active_scope_rows = self.store.db.all(
            """SELECT run_id,status,COUNT(*) AS scopes,SUM(records_seen) AS records
               FROM census_scopes
               WHERE run_id IN (SELECT id FROM census_runs ORDER BY id DESC LIMIT 10)
               GROUP BY run_id,status ORDER BY run_id DESC,status"""
        )
        return {
            "totals": dict(totals) if totals else {},
            "groups": [dict(row) for row in rows],
            "recent_runs": [dict(row) for row in runs],
            "recent_scope_status": [dict(row) for row in active_scope_rows],
        }

    def export_jsonl(self, output: Path) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        opener = gzip.open if output.suffix.casefold() == ".gz" else open
        count = 0
        with self.store.db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM census_records
                   ORDER BY source_type,house,parliament_number,session,record_id"""
            )
            with opener(output, "wt", encoding="utf-8") as handle:
                for row in rows:
                    payload = dict(row)
                    for field in ("members_json", "api_params_json", "raw_json"):
                        payload[field.removesuffix("_json")] = json.loads(payload.pop(field))
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    count += 1
        return count
