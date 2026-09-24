from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.classify_session import publication_ready, skip_reason
from scripts.plan_sessions import completed_scopes, valid_marker
from scripts.run_summary import REQUIRED_TRANCHE_FILES, is_session_complete, tranche_files_present


class MarkerSemanticsTests(unittest.TestCase):
    def test_failed_census_cannot_be_marked_empty(self):
        self.assertIsNone(skip_reason({"records": 0, "census_status": "failed"}))

    def test_only_proven_html_only_scope_is_skipped(self):
        base = {"records": 2, "acquired_documents": 0,
                "acquisition": {"failed": 2}, "census_status": "complete"}
        self.assertIsNone(skip_reason(base))
        self.assertIsNotNone(skip_reason({**base, "unsupported_html_records": 2}))
        self.assertIsNone(skip_reason({**base, "unsupported_html_records": 1}))

    def test_transient_acquisition_failures_remain_retryable(self):
        status = {"records": 3, "acquired_documents": 2,
                  "acquisition": {"failed": 1}, "census_status": "complete"}
        self.assertIsNone(skip_reason(status))

    def test_successful_zero_record_census_is_skipped(self):
        self.assertIsNotNone(skip_reason({"records": 0, "census_status": "complete"}))

    def test_planner_rejects_historical_unproven_skip_marker(self):
        path = "state/skipped/session-skipped-lok_sabha-p13-s3.json"
        marker = {"house": "lok_sabha", "parliament": "13", "session": "3",
                  "scope_status": {"records": 27, "acquisition": {"failed": 27}}}
        self.assertFalse(valid_marker(path, marker))
        marker["scope_status"].update(
            census_status="complete", unsupported_html_records=27,
            acquired_documents=0,
        )
        self.assertTrue(valid_marker(path, marker))
        self.assertFalse(valid_marker(path.replace("s3", "s4"), marker))

    def test_planner_requires_complete_marker_evidence(self):
        path = "state/complete/session-complete-lok_sabha-p18-s8.json"
        status = {"census_status": "complete", "records": 1,
                  "acquisition": {"downloaded": 1}, "acquired_documents": 1,
                  "processed_documents": 1, "pages": 2,
                  "openrouter_adjudicated_pages": 2}
        marker = {"inputs": {"house": "lok_sabha", "parliament": "18",
                             "session": "8", "limit": "0", "max_pages": "500",
                             "all_pages": "true"}, "scope_status": status,
                  "tranche_path": "data/tranche", "session_complete": True}
        self.assertTrue(valid_marker(path, marker))
        self.assertFalse(valid_marker(path, {**marker, "session_complete": False}))
        self.assertFalse(valid_marker(path, {**marker, "tranche_path": ""}))
        files = {f"data/tranche/{name}" for name in REQUIRED_TRANCHE_FILES}
        self.assertTrue(tranche_files_present(files, "data/tranche"))
        self.assertFalse(tranche_files_present(files - {"data/tranche/SHA256SUMS"},
                                               "data/tranche"))

    def test_planner_retries_when_completion_marker_outlives_tranche(self):
        marker_name = "state/complete/session-complete-lok_sabha-p18-s8.json"
        status = {"census_status": "complete", "records": 1,
                  "acquisition": {"downloaded": 1}, "acquired_documents": 1,
                  "processed_documents": 1, "pages": 2,
                  "openrouter_adjudicated_pages": 2}
        payload = {"inputs": {"house": "lok_sabha", "parliament": "18",
                              "session": "8", "limit": "0", "max_pages": "500",
                              "all_pages": "true"}, "scope_status": status,
                   "tranche_path": "data/tranche", "session_complete": True}
        with tempfile.TemporaryDirectory() as directory:
            marker_file = Path(directory) / "marker.json"
            marker_file.write_text(json.dumps(payload), encoding="utf-8")
            bundle_files = {f"data/tranche/{name}" for name in REQUIRED_TRANCHE_FILES}
            with patch("huggingface_hub.HfApi.list_repo_files",
                       return_value=[marker_name, *sorted(bundle_files)]), \
                 patch("huggingface_hub.hf_hub_download", return_value=str(marker_file)):
                self.assertEqual(completed_scopes("test/repo"), {"lok_sabha-p18-s8"})
            with patch("huggingface_hub.HfApi.list_repo_files",
                       return_value=[marker_name, *sorted(bundle_files - {
                           "data/tranche/webdataset/shard-00000.tar"})]), \
                 patch("huggingface_hub.hf_hub_download", return_value=str(marker_file)):
                self.assertEqual(completed_scopes("test/repo"), set())

    def test_completion_requires_page_coverage_and_published_tranche(self):
        status = {"census_status": "complete", "records": 3,
                  "acquisition": {"downloaded": 3}, "acquired_documents": 3,
                  "processed_documents": 3, "pages": 7,
                  "openrouter_adjudicated_pages": 7}
        options = {"tranche_path": "data/scope/tranche-1", "all_pages": True,
                   "unlimited": True, "adjudication_required": True}
        self.assertTrue(is_session_complete(status, **options))
        self.assertFalse(is_session_complete({**status, "openrouter_adjudicated_pages": 6}, **options))
        self.assertFalse(is_session_complete(status, **{**options, "tranche_path": ""}))
        self.assertFalse(is_session_complete(status, **{**options, "unlimited": False}))
        self.assertFalse(is_session_complete({**status, "processed_documents": 2}, **options))

    def test_publication_waits_for_all_pages(self):
        status = {"census_status": "complete", "acquired_documents": 2,
                  "records": 2,
                  "processed_documents": 2, "pages": 5,
                  "openrouter_adjudicated_pages": 4,
                  "acquisition": {"downloaded": 2}}
        self.assertFalse(publication_ready(status, limited=False, all_pages=True))
        self.assertTrue(publication_ready(
            {**status, "openrouter_adjudicated_pages": 5}, limited=False, all_pages=True
        ))

    def test_proven_html_non_pdf_records_do_not_hide_pdf_coverage(self):
        status = {"census_status": "complete", "records": 3,
                  "acquisition": {"downloaded": 2, "failed": 1},
                  "unsupported_html_records": 1,
                  "acquired_documents": 2, "processed_documents": 2,
                  "pages": 5, "openrouter_adjudicated_pages": 5}
        self.assertTrue(publication_ready(status, limited=False, all_pages=True))
        self.assertTrue(is_session_complete(
            status, tranche_path="data/tranche", all_pages=True,
            unlimited=True, adjudication_required=True,
        ))
        self.assertFalse(is_session_complete(
            {**status, "unsupported_html_records": 0}, tranche_path="data/tranche",
            all_pages=True, unlimited=True, adjudication_required=True,
        ))


if __name__ == "__main__":
    unittest.main()
