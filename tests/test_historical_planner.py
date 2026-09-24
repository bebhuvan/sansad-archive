from __future__ import annotations

import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import plan_elibrary_sessions
from scripts.plan_elibrary_sessions import roman_value, scope_counts
from scripts.run_summary import (
    REQUIRED_TRANCHE_FILES, elibrary_attachment_coverage, is_session_complete,
)
from sansad_pipeline.db import Database
from sansad_pipeline.sources.elibrary import normalize_session_label


class HistoricalPlannerTests(unittest.TestCase):
    def test_attachment_coverage_requires_every_item_and_selected_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            self.assertFalse(elibrary_attachment_coverage(
                path, house="lok_sabha", parliament="01", session="II")["complete"])
            db = Database(path)
            db.initialize()
            digest = "a" * 64
            db.execute("INSERT INTO documents VALUES (?,?,?,?,?)",
                       (digest, 12, "application/pdf", "/a.pdf", "now"))
            for record_id in ("elibrary_one", "elibrary_two"):
                db.execute(
                    """INSERT INTO census_records
                       (record_id,source_type,house,parliament_number,session,title,
                        language,source_url,official_page_url,api_url,api_params_json,
                        raw_json,discovered_at,acquisition_status,document_sha256)
                       VALUES (?,'questions_answers','lok_sabha','01','II','Question',
                               'und','','','','{}','{}','now','downloaded',?)""",
                    (record_id, digest),
                )
            db.execute(
                """INSERT INTO elibrary_pdf_attachments
                   (record_id,bitstream_id,position,name,source_url,
                    document_sha256,acquired_at)
                   VALUES ('elibrary_one','first',0,'first.pdf','url',?,'now')""",
                (digest,),
            )
            incomplete = elibrary_attachment_coverage(
                path, house="lok_sabha", parliament="01", session="II")
            self.assertEqual(incomplete["items_inventoried"], 1)
            self.assertFalse(incomplete["complete"])
            db.execute(
                """INSERT INTO elibrary_pdf_attachments
                   (record_id,bitstream_id,position,name,source_url,
                    document_sha256,acquired_at)
                   VALUES ('elibrary_two','second',0,'second.pdf','url',?,'now')""",
                (digest,),
            )
            self.assertTrue(elibrary_attachment_coverage(
                path, house="lok_sabha", parliament="01", session="II")["complete"])
            self.assertTrue(elibrary_attachment_coverage(
                Path(os.path.relpath(path)), house="lok_sabha",
                parliament="01", session="II")["complete"])

    def test_snapshot_scopes_require_hash_and_session_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "snapshot.jsonl.gz"
            with gzip.open(archive, "wt", encoding="utf-8") as stream:
                for parliament, session in (("01", "I"), ("01", "I"), ("01", "II")):
                    stream.write(json.dumps({
                        "record_id": "elibrary_test", "house": "lok_sabha",
                        "parliament_number": parliament, "session": session,
                    }) + "\n")
                stream.write(json.dumps({
                    "record_id": "current_test", "house": "lok_sabha",
                    "parliament_number": "01", "session": "I",
                }) + "\n")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(scope_counts(archive, digest), {("01", "I"): 2, ("01", "II"): 1})
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                scope_counts(archive, "0" * 64)
            self.assertLess(roman_value("IV"), roman_value("VIII"))
            self.assertEqual(roman_value("14"), 14)
            with self.assertRaisesRegex(ValueError, "unexpected eLibrary session"):
                roman_value("Anandgajapati RajuI")

    def test_exact_member_prefix_repair_preserves_source_value(self):
        source = "Anandgajapati RajuI"
        normalized, evidence = normalize_session_label(source, ["Anandgajapati Raju"])
        self.assertEqual(normalized, "I")
        self.assertEqual(evidence, {
            "original": source, "normalized": "I", "rule": "exact-listed-member-prefix",
        })
        self.assertEqual(normalize_session_label(source, ["Someone Else"]), (source, None))

    def test_dated_completion_requires_full_acquisition_and_model_coverage(self):
        status = {
            "census_status": "complete", "records": 2,
            "acquisition": {"downloaded": 2, "discovered": 0, "failed": 0},
            "acquired_documents": 2, "processed_documents": 2, "pages": 3,
            "openrouter_adjudicated_pages": 3,
        }
        kwargs = dict(tranche_path="data/test", all_pages=True, unlimited=True,
                      adjudication_required=True)
        self.assertTrue(is_session_complete(status, **kwargs))
        status["openrouter_adjudicated_pages"] = 2
        self.assertFalse(is_session_complete(status, **kwargs))

    def test_only_evidence_backed_marker_with_matching_snapshot_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "marker.json"
            status = {
                "census_status": "complete", "records": 2,
                "acquisition": {"downloaded": 2, "discovered": 0, "failed": 0},
                "acquired_documents": 2, "processed_documents": 2,
                "pages": 3, "openrouter_adjudicated_pages": 3,
            }
            payload = {
                "snapshot_complete": True, "session_complete": False,
                "attachment_complete": True,
                "census_snapshot": {"sha256": "abc"}, "tranche_path": "data/test",
                "inputs": {"source": "elibrary", "parliament": "01", "session": "I",
                           "all_pages": "true", "limit": "0", "max_pages": "500"},
                "scope_status": status,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            marker = "state/snapshot-complete/snapshot-complete-lok_sabha-p01-sI.json"
            bundle_files = {f"data/test/{name}" for name in REQUIRED_TRANCHE_FILES}
            with patch.object(plan_elibrary_sessions.HfApi, "list_repo_files",
                              return_value=[marker, *sorted(bundle_files)]), \
                 patch.object(plan_elibrary_sessions, "hf_hub_download", return_value=str(path)):
                self.assertEqual(plan_elibrary_sessions.completed_scopes("test/repo", "abc"), {("01", "I")})
                self.assertEqual(plan_elibrary_sessions.completed_scopes("test/repo", "def"), set())
                del payload["attachment_complete"]
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(plan_elibrary_sessions.completed_scopes("test/repo", "abc"), set())
                payload["attachment_complete"] = True
                status["openrouter_adjudicated_pages"] = 2
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(plan_elibrary_sessions.completed_scopes("test/repo", "abc"), set())
            with patch.object(plan_elibrary_sessions.HfApi, "list_repo_files",
                              return_value=[marker, *sorted(bundle_files - {
                                  "data/test/manifest.jsonl"})]), \
                 patch.object(plan_elibrary_sessions, "hf_hub_download", return_value=str(path)):
                status["openrouter_adjudicated_pages"] = 3
                path.write_text(json.dumps(payload), encoding="utf-8")
                self.assertEqual(plan_elibrary_sessions.completed_scopes("test/repo", "abc"), set())


if __name__ == "__main__":
    unittest.main()
