from __future__ import annotations

import unittest

from scripts.classify_session import publication_ready, skip_reason
from scripts.run_summary import is_session_complete


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

    def test_completion_requires_page_coverage_and_published_tranche(self):
        status = {"census_status": "complete", "records": 3,
                  "acquisition": {"acquired": 3}, "acquired_documents": 3,
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
                  "processed_documents": 2, "pages": 5,
                  "openrouter_adjudicated_pages": 4,
                  "acquisition": {"acquired": 2}}
        self.assertFalse(publication_ready(status, limited=False, all_pages=True))
        self.assertTrue(publication_ready(
            {**status, "openrouter_adjudicated_pages": 5}, limited=False, all_pages=True
        ))


if __name__ == "__main__":
    unittest.main()
