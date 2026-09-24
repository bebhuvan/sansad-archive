from __future__ import annotations

import gzip
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sansad_pipeline.census import Census
from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.sources.questions import QuestionRecord


class CensusImportTests(unittest.TestCase):
    def test_verified_slice_import_is_idempotent_and_source_filtered(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "snapshot.jsonl.gz"
            base = dict(
                source_type="questions_answers", house="lok_sabha",
                parliament_number="01", session="I", document_number="1",
                document_subtype="UNSTARRED", document_date="1952-07-28",
                title="Example", ministry="DEFENCE", members=[], language="en",
                source_url="https://example.test/item", official_page_url="https://example.test",
                api_url="https://example.test/api", api_params={}, raw={},
            )
            records = [
                QuestionRecord(record_id="elibrary_one", **base).metadata(),
                QuestionRecord(record_id="ls_current_one", **base).metadata(),
                QuestionRecord(record_id="elibrary_two", **base).metadata(),
            ]
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record) + "\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            census = Census(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            for _ in range(2):
                result = census.import_snapshot(
                    path, source="elibrary", offset=1, limit=1,
                    expected_sha256=digest,
                )
                self.assertEqual(result["imported"], 1)
            rows = census.store.db.all("SELECT record_id,acquisition_status FROM census_records")
            self.assertEqual([(r["record_id"], r["acquisition_status"]) for r in rows],
                             [("elibrary_two", "discovered")])
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                census.import_snapshot(path, expected_sha256="0" * 64)
            self.assertEqual(census.store.db.one("SELECT COUNT(*) n FROM census_records")["n"], 1)

    def test_malformed_selected_record_fails_visibly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "bad.jsonl"
            path.write_text('{"record_id":"elibrary_one","house":"lok_sabha"}\n')
            census = Census(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            with self.assertRaisesRegex(RuntimeError, "line 1"):
                census.import_snapshot(path, source="elibrary")

    def test_full_dated_scope_import_records_snapshot_census_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "scope.jsonl"
            record = QuestionRecord(
                record_id="elibrary_one", source_type="questions_answers",
                house="lok_sabha", parliament_number="01", session="I",
                document_number="1", document_subtype="UNSTARRED",
                document_date="1952-07-28", title="Example", ministry="DEFENCE",
                members=[], language="en", source_url="https://example.test/item",
                official_page_url="https://example.test", api_url="https://example.test/api",
                api_params={}, raw={},
            ).metadata()
            path.write_text(json.dumps(record) + "\n")
            census = Census(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            census.import_snapshot(
                path, source="elibrary", house="lok_sabha",
                parliament="01", session="I",
            )
            scope = census.store.db.one(
                "SELECT status,records_seen FROM census_scopes WHERE house='lok_sabha'"
            )
            self.assertEqual((scope["status"], scope["records_seen"]), ("complete", 1))


if __name__ == "__main__":
    unittest.main()
