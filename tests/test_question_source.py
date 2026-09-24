from __future__ import annotations

import unittest
from unittest.mock import patch

from sansad_pipeline.sources.questions import (
    available_lok_sabha_sessions, discover_lok_sabha_questions,
    discover_rajya_sabha_questions, parse_date,
)


class QuestionSourceTests(unittest.TestCase):
    @patch("sansad_pipeline.sources.questions.request_json")
    def test_lok_sabha_inventory_is_sorted_and_unique(self, request_json):
        request_json.return_value = [
            {"loksabha": 18, "sessions": [{"sessionNo": 2}, {"sessionNo": 1}]},
            {"loksabha": 18, "sessions": [{"sessionNo": 1}]},
        ]
        self.assertEqual(available_lok_sabha_sessions(), [("18", "1"), ("18", "2")])

    @patch("sansad_pipeline.sources.questions.request_json")
    def test_lok_sabha_census_rejects_missing_page(self, request_json):
        request_json.side_effect = [
            [{"totalRecordSize": 2, "listOfQuestions": [{"quesNo": 1, "type": "S"}]}],
            [{"totalRecordSize": 2, "listOfQuestions": []}],
        ]
        with self.assertRaisesRegex(RuntimeError, "ended after 1 of 2"):
            list(discover_lok_sabha_questions("18", "8", page_size=1, sleep_seconds=0))

    @patch("sansad_pipeline.sources.questions.request_json")
    def test_lok_sabha_census_retains_missing_pdf_link_as_failure_evidence(self, request_json):
        request_json.return_value = [
            {"totalRecordSize": 1, "listOfQuestions": [{"quesNo": 1, "type": "S"}]}
        ]
        records = list(discover_lok_sabha_questions("18", "8", sleep_seconds=0))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_url, "")

    def test_official_date_format(self):
        self.assertEqual(parse_date("02.04.2026"), "2026-04-02")

    def test_unknown_date_is_empty(self):
        self.assertEqual(parse_date("date unavailable"), "")

    @patch("sansad_pipeline.sources.questions.request_json")
    def test_rajya_sabha_duplicate_members_are_grouped(self, request_json):
        base = {
            "qslno": 123,
            "qtitle": "A subject",
            "qtype": "UNSTARRED ",
            "ans_date": "02.04.2026",
            "qno": 4455.0,
            "min_name": "SCIENCE ",
            "ses_no": 270,
            "files": "https://sansad.in/getFile/example.pdf",
            "shri": "Shri",
        }
        request_json.return_value = [
            {**base, "name": "First Member"},
            {**base, "name": "Second Member"},
        ]
        records = list(discover_rajya_sabha_questions("270"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].document_number, "4455")
        self.assertEqual(records[0].members, ["Shri First Member", "Shri Second Member"])


if __name__ == "__main__":
    unittest.main()
