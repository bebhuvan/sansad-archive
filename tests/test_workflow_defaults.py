from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/digitize-batch.yml"


class ScheduledBatchDefaultsTests(unittest.TestCase):
    def test_scheduled_batch_keeps_full_coverage_and_skips_completed_scopes(self):
        lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
        for setting, input_name in (
            ("SKIP_EXISTING", "skip_existing"),
            ("all_pages", "all_pages"),
            ("include_ocr", "include_ocr"),
        ):
            with self.subTest(setting=setting):
                matches = [line.strip() for line in lines
                           if line.strip().startswith(f"{setting}: ${{{{")]
                self.assertEqual(len(matches), 1)
                expression = matches[0]
                self.assertIn("github.event_name == 'schedule'", expression)
                self.assertIn(f"inputs.{input_name} == true", expression)
                self.assertIn(f"inputs.{input_name} == 'true'", expression)
                self.assertIn("&& 'true' || 'false'", expression)


if __name__ == "__main__":
    unittest.main()
