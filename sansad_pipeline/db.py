from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS documents (
    sha256 TEXT PRIMARY KEY,
    size_bytes INTEGER NOT NULL,
    media_type TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    document_sha256 TEXT NOT NULL REFERENCES documents(sha256),
    source_uri TEXT NOT NULL,
    original_name TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(document_sha256, source_uri)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    document_sha256 TEXT NOT NULL REFERENCES documents(sha256),
    status TEXT NOT NULL,
    config_json TEXT NOT NULL,
    artifact_dir TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    route TEXT NOT NULL,
    engine TEXT NOT NULL,
    text_chars INTEGER NOT NULL,
    mean_confidence REAL,
    validation_status TEXT NOT NULL,
    flags_json TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    PRIMARY KEY(run_id, page_number)
);
CREATE INDEX IF NOT EXISTS idx_runs_document ON runs(document_sha256, id);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(validation_status);
CREATE TABLE IF NOT EXISTS adjudications (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id),
    page_number INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_path TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    total_tokens INTEGER,
    reported_cost REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_adjudications_page_provider
ON adjudications(run_id, page_number, provider, id);
CREATE TABLE IF NOT EXISTS census_runs (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    records_seen INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS census_records (
    record_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    house TEXT NOT NULL,
    parliament_number TEXT,
    session TEXT,
    document_number TEXT,
    document_subtype TEXT,
    document_date TEXT,
    title TEXT NOT NULL,
    ministry TEXT,
    members_json TEXT NOT NULL DEFAULT '[]',
    language TEXT NOT NULL,
    source_url TEXT NOT NULL,
    official_page_url TEXT NOT NULL,
    api_url TEXT NOT NULL,
    api_params_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    acquisition_status TEXT NOT NULL DEFAULT 'discovered',
    document_sha256 TEXT REFERENCES documents(sha256),
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_census_scope
ON census_records(source_type, house, parliament_number, session, acquisition_status);
CREATE TABLE IF NOT EXISTS census_scopes (
    run_id INTEGER NOT NULL REFERENCES census_runs(id),
    house TEXT NOT NULL,
    parliament_number TEXT NOT NULL,
    session TEXT NOT NULL,
    status TEXT NOT NULL,
    records_seen INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error TEXT,
    PRIMARY KEY(run_id, house, parliament_number, session)
);
"""

_INITIALIZE_LOCK = threading.Lock()



class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _INITIALIZE_LOCK:
            with self.connect() as connection:
                connection.executescript(SCHEMA)
                indexes = connection.execute("PRAGMA index_list(census_records)").fetchall()
                legacy_unique = any(
                    row[2] == 1
                    and [
                        item[2]
                        for item in connection.execute(f"PRAGMA index_info('{row[1]}')").fetchall()
                    ]
                    == ["source_url", "language"]
                    for row in indexes
                )
                if legacy_unique:
                    connection.executescript(
                    """
                    ALTER TABLE census_records RENAME TO census_records_legacy;
                    CREATE TABLE census_records (
                        record_id TEXT PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        house TEXT NOT NULL,
                        parliament_number TEXT,
                        session TEXT,
                        document_number TEXT,
                        document_subtype TEXT,
                        document_date TEXT,
                        title TEXT NOT NULL,
                        ministry TEXT,
                        members_json TEXT NOT NULL DEFAULT '[]',
                        language TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        official_page_url TEXT NOT NULL,
                        api_url TEXT NOT NULL,
                        api_params_json TEXT NOT NULL,
                        raw_json TEXT NOT NULL,
                        discovered_at TEXT NOT NULL,
                        acquisition_status TEXT NOT NULL DEFAULT 'discovered',
                        document_sha256 TEXT REFERENCES documents(sha256),
                        last_error TEXT
                    );
                    INSERT INTO census_records SELECT * FROM census_records_legacy;
                    DROP TABLE census_records_legacy;
                    CREATE INDEX idx_census_scope
                    ON census_records(source_type, house, parliament_number, session, acquisition_status);
                    """
                    )
                connection.executescript(
                    """CREATE TABLE IF NOT EXISTS elibrary_pdf_attachments (
                           record_id TEXT NOT NULL REFERENCES census_records(record_id),
                           bitstream_id TEXT NOT NULL,
                           position INTEGER NOT NULL,
                           name TEXT NOT NULL,
                           source_url TEXT NOT NULL,
                           document_sha256 TEXT NOT NULL REFERENCES documents(sha256),
                           acquired_at TEXT NOT NULL,
                           PRIMARY KEY(record_id, bitstream_id)
                       );
                       CREATE INDEX IF NOT EXISTS idx_elibrary_pdf_attachments_record
                       ON elibrary_pdf_attachments(record_id, position);"""
                )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.connect() as connection:
            cursor = connection.execute(sql, params)
            return int(cursor.lastrowid or 0)

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, params).fetchall())


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
