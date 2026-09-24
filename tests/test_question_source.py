from __future__ import annotations

import unittest
from unittest.mock import patch

from sansad_pipeline.sources.questions import discover_rajya_sabha_questions, parse_date


class QuestionSourceTests(unittest.TestCase):
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
