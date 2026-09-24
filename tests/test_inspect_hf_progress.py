from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import zstandard

from sansad_pipeline.db import Database
from scripts.inspect_hf_progress import (
    extract_database, inspect, raw_pdf_count, summarize_database, verify_archive,
)


class CheckpointMonitorTests(unittest.TestCase):
    def test_monitor_uses_temporary_state_and_keeps_original_shards_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "fixture.sqlite3"
            db = Database(db_path)
            db.initialize()
            digest = "a" * 64
            db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?)",
                (digest, 12, "application/pdf", "/not-downloaded.pdf", "now"),
            )
            superseded_run = db.execute(
                """INSERT INTO runs(document_sha256,status,config_json,started_at)
                   VALUES (?,'complete','{}','before')""", (digest,),
            )
            db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (superseded_run, 1, "native", "liteparse", 10, None, "review",
                 '["empty-text"]', "old.json"),
            )
            run_id = db.execute(
                """INSERT INTO runs(document_sha256,status,config_json,started_at)
                   VALUES (?,'complete','{}','now')""", (digest,),
            )
            db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, 1, "ocr", "liteparse", 25, None, "accepted", "[]", "page.json"),
            )
            db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, 2, "ocr", "liteparse", 12, 42.0, "review",
                 '["low-ocr-confidence","native-ocr-numeric-disagreement"]', "page2.json"),
            )
            db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,
                    reported_cost,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, 1, "openrouter", "free/test", "hash", "response.json", 0, "now"),
            )
            db.execute(
                """INSERT INTO census_records
                   (record_id,source_type,house,parliament_number,session,title,language,
                    source_url,official_page_url,api_url,api_params_json,raw_json,
                    discovered_at,acquisition_status,document_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("one", "questions_answers", "lok_sabha", "18", "8", "Test", "English",
                 "", "", "", "{}", "{}", "now", "downloaded", digest),
            )
            unrelated_digest = "b" * 64
            db.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?)",
                (unrelated_digest, 12, "application/pdf", "/other.pdf", "now"),
            )
            unrelated_run = db.execute(
                """INSERT INTO runs(document_sha256,status,config_json,started_at)
                   VALUES (?,'complete','{}','now')""", (unrelated_digest,),
            )
            db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,
                    reported_cost,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (unrelated_run, 1, "openrouter", "paid/other", "hash", "other.json", 1, "now"),
            )
            db.execute(
                """INSERT INTO census_records
                   (record_id,source_type,house,parliament_number,session,title,language,
                    source_url,official_page_url,api_url,api_params_json,raw_json,
                    discovered_at,acquisition_status,document_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("other", "questions_answers", "lok_sabha", "18", "7", "Other", "English",
                 "", "", "", "{}", "{}", "now", "downloaded", unrelated_digest),
            )
            raw_tar = root / "state.tar"
            with tarfile.open(raw_tar, "w") as archive:
                archive.add(db_path, arcname="data/pipeline.sqlite3")
            state = root / "state.tar.zst"
            with raw_tar.open("rb") as source, state.open("wb") as target:
                zstandard.ZstdCompressor().copy_stream(source, target)
            state_info = {
                "path": "state/checkpoints/lok_sabha-p18-s8/state.tar.zst",
                "bytes": state.stat().st_size,
                "sha256": hashlib.sha256(state.read_bytes()).hexdigest(),
            }
            manifest = root / "checkpoint.json"
            manifest.write_text(json.dumps({
                "version": 2, "created_at": "2026-09-24T20:00:00Z",
                "state": state_info, "raw": {"files": 1},
            }), encoding="utf-8")
            locations = []

            def fake_download(repo, filename, *, repo_type, local_dir, force_download):
                self.assertEqual(repo, "test/corpus")
                self.assertEqual(repo_type, "dataset")
                self.assertTrue(force_download)
                locations.append(Path(local_dir))
                source = manifest if filename.endswith("checkpoint.json") else state
                destination = Path(local_dir) / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                return str(destination)

            with patch("huggingface_hub.hf_hub_download", side_effect=fake_download):
                result = inspect("test/corpus", "lok_sabha-p18-s8", max_db_bytes=1024 * 1024)
                with self.assertRaisesRegex(RuntimeError, "state archive exceeds"):
                    inspect("test/corpus", "lok_sabha-p18-s8", max_db_bytes=1024 * 1024,
                            max_state_bytes=1)
            self.assertEqual(result["retained_original_pdfs"], 1)
            self.assertEqual(result["source_records_by_acquisition_status"], {"downloaded": 1})
            self.assertEqual(result["processed_pdfs"], 1)
            self.assertEqual(result["pages_by_route"], {"ocr": 2})
            self.assertEqual(result["pages_by_validation_status"], {"accepted": 1, "review": 1})
            self.assertEqual(result["validation_flag_counts"], {
                "low-ocr-confidence": 1, "native-ocr-numeric-disagreement": 1,
            })
            self.assertEqual(result["model_pages"], 1)
            self.assertEqual(result["cost_zero_calls"], 1)
            self.assertEqual(result["cost_nonzero_calls"], 0)
            self.assertEqual(result["model_calls"], 1)
            self.assertTrue(all(not location.exists() for location in locations))
            verify_archive(state, state_info)
            with self.assertRaisesRegex(RuntimeError, "disk limit"):
                extract_database(state, root / "too-small.sqlite3", max_bytes=1)
            with self.assertRaisesRegex(RuntimeError, "SHA-256 mismatch"):
                verify_archive(state, {**state_info, "sha256": "0" * 64})

    def test_v3_raw_index_count_and_scope_validation(self):
        self.assertEqual(raw_pdf_count({"version": 3, "raw_index": {"a": {}, "b": {}}}), 2)
        self.assertEqual(raw_pdf_count({"version": 2, "raw": {"files": 3}}), 3)
        with self.assertRaises(ValueError):
            raw_pdf_count({"version": 1})
        with self.assertRaisesRegex(ValueError, "invalid checkpoint scope"):
            summarize_database(Path("unused.sqlite3"), "../../other")


if __name__ == "__main__":
    unittest.main()
