from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from scripts.audit_published_ocr import audit_layers, publish_report


class PublishedOcrAuditTests(unittest.TestCase):
    def test_blank_image_conflicts_are_reported_without_changing_layers(self):
        digest = "a" * 64
        pages = [{"document_sha256": digest, "page_number": 1,
                  "local_text": "", "local_markdown": "```text\n\n```",
                  "adjudicated_markdown": "Invented model text"}]
        ocr = [{"document_sha256": digest, "page_number": 1,
                "text": "Invented OCR text", "visually_blank": True,
                "image_pixels": 10000, "image_dark_pixels": 0,
                "image_dark_pixel_cutoff": 250}]
        result = audit_layers(pages, ocr)
        self.assertEqual(result["visually_assessed_pages"], 1)
        self.assertEqual(result["conflict_counts"], {
            "ocr-nonempty-on-blank": 1, "local-nonempty-on-blank": 0,
            "model-nonempty-on-blank": 1,
        })
        self.assertEqual(pages[0]["adjudicated_markdown"], "Invented model text")

    def test_legacy_shard_is_unassessed_not_clean(self):
        digest = "a" * 64
        pages = [{"document_sha256": digest, "page_number": 1,
                  "local_text": "", "local_markdown": "",
                  "adjudicated_markdown": "Invented text"}]
        ocr = [{"document_sha256": digest, "page_number": 1, "text": "OCR text"}]
        result = audit_layers(pages, ocr)
        self.assertEqual(result["ocr_pages_without_visual_metrics"], 1)
        self.assertEqual(result["visually_assessed_pages"], 0)
        self.assertEqual(result["conflict_counts"]["model-nonempty-on-blank"], 0)

    def test_backfill_assesses_legacy_ocr_and_pages_not_yet_ocr_processed(self):
        digest = "a" * 64
        pages = [
            {"document_sha256": digest, "page_number": number,
             "local_text": "", "local_markdown": "",
             "adjudicated_markdown": "Invented model text"}
            for number in (1, 2)
        ]
        ocr = [{"document_sha256": digest, "page_number": 1,
                "text": "Invented OCR text"}]
        visual = [
            {"document_sha256": digest, "page_number": number,
             "image_pixels": 10000, "image_dark_pixels": 0,
             "image_dark_pixel_cutoff": 250, "visually_blank": True}
            for number in (1, 2)
        ]
        result = audit_layers(pages, ocr, visual)
        self.assertEqual(result["visually_assessed_pages"], 2)
        self.assertEqual(result["ocr_pages_without_visual_metrics"], 0)
        self.assertEqual(result["conflict_counts"], {
            "ocr-nonempty-on-blank": 1, "local-nonempty-on-blank": 0,
            "model-nonempty-on-blank": 2,
        })

    def test_disagreeing_ocr_and_backfill_pixels_fail_closed(self):
        digest = "a" * 64
        pages = [{"document_sha256": digest, "page_number": 1,
                  "local_text": "", "local_markdown": "",
                  "adjudicated_markdown": ""}]
        ocr = [{"document_sha256": digest, "page_number": 1, "text": "",
                "image_pixels": 10000, "image_dark_pixels": 0,
                "image_dark_pixel_cutoff": 250, "visually_blank": True}]
        visual = [{"document_sha256": digest, "page_number": 1,
                   "image_pixels": 10000, "image_dark_pixels": 1,
                   "image_dark_pixel_cutoff": 250, "visually_blank": True}]
        with self.assertRaisesRegex(RuntimeError, "disagrees"):
            audit_layers(pages, ocr, visual)

    def test_mismatched_identity_and_bad_metrics_fail_closed(self):
        pages = [{"document_sha256": "a" * 64, "page_number": 1,
                  "local_text": "", "local_markdown": "",
                  "adjudicated_markdown": ""}]
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            audit_layers(pages, [{"document_sha256": "b" * 64,
                                  "page_number": 1, "text": ""}])
        with self.assertRaisesRegex(RuntimeError, "invalid OCR visual evidence"):
            audit_layers(pages, [{"document_sha256": "a" * 64,
                                  "page_number": 1, "text": "",
                                  "visually_blank": True,
                                  "image_pixels": 10000, "image_dark_pixels": 100,
                                  "image_dark_pixel_cutoff": 250}])

    def test_report_publish_verifies_remote_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            report.write_bytes(b'{"conflicts": []}\n')
            api = mock.Mock()
            api.get_paths_info.return_value = []
            with mock.patch("scripts.audit_published_ocr._commit_with_retry") as commit, \
                 mock.patch("scripts.audit_published_ocr.hf_hub_download",
                            return_value=str(report)):
                path = publish_report("repo", "lok_sabha-p17-s15", report.read_bytes(),
                                      100, "token", api)
            self.assertIn("ocr-pages-00000100-", path)
            commit.assert_called_once()
            report.write_bytes(b'{"conflicts": ["changed"]}\n')
            with mock.patch("scripts.audit_published_ocr.hf_hub_download",
                            return_value=str(report)):
                with self.assertRaisesRegex(RuntimeError, "differs"):
                    publish_report("repo", "lok_sabha-p17-s15", b'{"conflicts": []}\n',
                                   100, "token", api)


if __name__ == "__main__":
    unittest.main()
