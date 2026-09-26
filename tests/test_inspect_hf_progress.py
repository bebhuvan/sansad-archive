from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import zstandard
import httpx
from huggingface_hub.errors import RemoteEntryNotFoundError

from sansad_pipeline.db import Database
from scripts.inspect_hf_progress import (
    audit_transcript_archive, extract_database, inspect, raw_pdf_count,
    summarize_database, verify_archive,
)


class CheckpointMonitorTests(unittest.TestCase):
    def test_historical_attachment_coverage_is_distinct_from_selected_pdfs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            db = Database(path)
            db.initialize()
            selected = "a" * 64
            extra = "b" * 64
            for digest in (selected, extra):
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?)",
                           (digest, 12, "application/pdf", f"/{digest}.pdf", "now"))
            for record_id in ("elibrary_one", "elibrary_two"):
                db.execute(
                    """INSERT INTO census_records
                       (record_id,source_type,house,parliament_number,session,title,
                        language,source_url,official_page_url,api_url,api_params_json,
                        raw_json,discovered_at,acquisition_status,document_sha256)
                       VALUES (?,'questions_answers','lok_sabha','01','II','Question',
                               'und','','','','{}','{}','now','downloaded',?)""",
                    (record_id, selected),
                )
            for position, (bitstream, digest) in enumerate((("selected", selected),
                                                             ("extra", extra))):
                db.execute(
                    """INSERT INTO elibrary_pdf_attachments
                       (record_id,bitstream_id,position,name,source_url,
                        document_sha256,acquired_at) VALUES (?,?,?,?,?,?,?)""",
                    ("elibrary_one", bitstream, position, f"{bitstream}.pdf",
                     f"https://example.test/{bitstream}", digest, "now"),
                )
            result = summarize_database(path, "elibrary-lok_sabha-p01-sII")
            self.assertEqual(result["attachment_inventory_status"], "present")
            self.assertEqual(result["attachment_items_total"], 2)
            self.assertEqual(result["attachment_items_inventoried"], 1)
            self.assertEqual(result["attachment_items_missing_inventory"], 1)
            self.assertEqual(result["attachment_pdf_bitstreams"], 2)
            self.assertEqual(result["attachment_additional_pdf_bitstreams"], 1)
            db.execute("DROP TABLE elibrary_pdf_attachments")
            legacy = summarize_database(path, "elibrary-lok_sabha-p01-sII")
            self.assertEqual(legacy["attachment_inventory_status"], "not_recorded")

    def test_missing_checkpoint_is_reported_without_hiding_other_hub_errors(self):
        with patch("huggingface_hub.HfApi.repo_info",
                   return_value=SimpleNamespace(sha="a" * 40)), patch("huggingface_hub.hf_hub_download",
                   side_effect=RemoteEntryNotFoundError(
                       "missing", response=httpx.Response(
                           404, request=httpx.Request("GET", "https://example.test/missing")))):
            result = inspect("test/corpus", "elibrary-lok_sabha-p01-sIII",
                             max_db_bytes=1024 * 1024)
        self.assertEqual(result["checkpoint_status"], "not_found")
        self.assertIsNone(result["checkpoint_at"])
        with patch("huggingface_hub.HfApi.repo_info",
                   return_value=SimpleNamespace(sha="a" * 40)), patch("huggingface_hub.hf_hub_download",
                   side_effect=RuntimeError("hub unavailable")):
            with self.assertRaisesRegex(RuntimeError, "hub unavailable"):
                inspect("test/corpus", "elibrary-lok_sabha-p01-sIII",
                        max_db_bytes=1024 * 1024)

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
            transcript_path = root / "data" / "artifacts" / digest / "adjudicated.md"
            transcript_path.parent.mkdir(parents=True)
            transcript_path.write_text("Model reading", encoding="utf-8")
            db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,
                    reported_cost,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, 1, "openrouter", "free/test", "hash",
                 str(transcript_path.with_name("response.json")), 0, "now"),
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
                archive.add(transcript_path,
                            arcname=f"data/artifacts/{digest}/adjudicated.md")
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

            def fake_download(repo, filename, *, repo_type, revision, local_dir,
                              force_download):
                self.assertEqual(repo, "test/corpus")
                self.assertEqual(repo_type, "dataset")
                self.assertEqual(revision, "a" * 40)
                self.assertTrue(force_download)
                locations.append(Path(local_dir))
                source = manifest if filename.endswith("checkpoint.json") else state
                destination = Path(local_dir) / filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                return str(destination)

            with patch("huggingface_hub.HfApi.repo_info",
                       return_value=SimpleNamespace(sha="a" * 40)), patch(
                           "huggingface_hub.hf_hub_download", side_effect=fake_download):
                result = inspect("test/corpus", "lok_sabha-p18-s8", max_db_bytes=1024 * 1024)
                audited = inspect("test/corpus", "lok_sabha-p18-s8",
                                  max_db_bytes=1024 * 1024, audit_transcripts=True)
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
            self.assertEqual(audited["model_transcript_artifacts_expected"], 1)
            self.assertEqual(audited["model_transcript_artifacts_missing"], 0)
            self.assertEqual(audited["model_transcript_artifacts_blank_or_invalid"], 0)
            self.assertEqual(audited["model_transcript_artifacts_verified_blank"], 0)
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

    def test_archive_audit_distinguishes_missing_and_blank_transcripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blank = root / "blank.md"
            blank.write_text("  \n", encoding="utf-8")
            raw_tar = root / "state.tar"
            with tarfile.open(raw_tar, "w") as archive:
                archive.add(blank, arcname="data/artifacts/blank/adjudicated.md")
            state = root / "state.tar.zst"
            with raw_tar.open("rb") as source, state.open("wb") as target:
                zstandard.ZstdCompressor().copy_stream(source, target)
            audit = audit_transcript_archive(state, {
                "data/artifacts/blank/adjudicated.md",
                "data/artifacts/missing/adjudicated.md",
            })
            self.assertEqual(audit, {
                "model_transcript_artifacts_expected": 2,
                "model_transcript_artifacts_missing": 1,
                "model_transcript_artifacts_blank_or_invalid": 1,
                "model_transcript_artifacts_verified_blank": 0,
            })

    def test_archive_audit_accepts_empty_model_only_with_blank_image_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            transcript = root / "adjudicated.md"
            transcript.write_text("", encoding="utf-8")
            provenance = root / "provenance.json"
            provenance.write_text(json.dumps({
                "blank_response_verified": True,
                "visual_quality": {"visually_blank": True, "image_pixels": 10000,
                                   "image_dark_pixels": 0, "image_dark_pixel_cutoff": 250},
            }))
            raw_tar = root / "state.tar"
            with tarfile.open(raw_tar, "w") as archive:
                archive.add(transcript, arcname="data/artifacts/blank/adjudicated.md")
                archive.add(provenance, arcname="data/artifacts/blank/provenance.json")
            state = root / "state.tar.zst"
            with raw_tar.open("rb") as source, state.open("wb") as target:
                zstandard.ZstdCompressor().copy_stream(source, target)
            result = audit_transcript_archive(state, {"data/artifacts/blank/adjudicated.md"})
            self.assertEqual(result["model_transcript_artifacts_blank_or_invalid"], 0)
            self.assertEqual(result["model_transcript_artifacts_verified_blank"], 1)


if __name__ == "__main__":
    unittest.main()
