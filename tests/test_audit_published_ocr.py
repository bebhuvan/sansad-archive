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

    def test_empty_model_on_visibly_inked_page_is_reviewed_not_called_blank(self):
        digest = "a" * 64
        pages = [
            {"document_sha256": digest, "page_number": number,
             "local_text": "Question", "local_markdown": "Question",
             "adjudicated_markdown": ""}
            for number in (1, 2)
        ]
        visual = [{"document_sha256": digest, "page_number": 1,
                   "image_pixels": 10000, "image_dark_pixels": 100,
                   "image_dark_pixel_cutoff": 250, "visually_blank": False}]
        result = audit_layers(pages, [], visual)
        self.assertEqual(result["model_empty_on_visible_pages"], 1)
        self.assertEqual(result["model_empty_on_visible"][0]["page_number"], 1)
        self.assertEqual(result["published_pages_without_visual_metrics"], 1)
        self.assertEqual(result["visually_blank_pages_among_assessed"], 0)

    def test_empty_ocr_wrapper_on_visible_page_is_reviewed_not_numeric_witness(self):
        digest = "a" * 64
        pages = [{"document_sha256": digest, "page_number": 1,
                  "local_text": "*****", "local_markdown": "*****",
                  "adjudicated_markdown": "*****"}]
        ocr = [{"document_sha256": digest, "page_number": 1,
                "text": "", "markdown": "```text\n\n```",
                "quality_flags": ["ocr-empty-on-visibly-nonblank-page"],
                "image_pixels": 10000, "image_dark_pixels": 531,
                "image_dark_pixel_cutoff": 250, "visually_blank": False}]
        result = audit_layers(pages, ocr)
        self.assertEqual(result["ocr_empty_on_visible_pages"], 1)
        self.assertEqual(result["ocr_model_numeric_pages_compared"], 0)
        self.assertEqual(result["numeric_triad_pages_compared"], 0)

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

    def test_numeric_comparison_is_separate_from_visual_conflicts(self):
        digest = "a" * 64
        pages = [
            {"document_sha256": digest, "page_number": 1,
             "local_text": "", "local_markdown": "",
             "adjudicated_markdown": "Question 42\nPage 1 of 2"},
            {"document_sha256": digest, "page_number": 2,
             "local_text": "", "local_markdown": "",
             "adjudicated_markdown": "Question 43"},
        ]
        ocr = [
            {"document_sha256": digest, "page_number": 1,
             "text": "Question 42", "markdown": "Question 42"},
            {"document_sha256": digest, "page_number": 2,
             "text": "Question 44", "markdown": "Question 44"},
        ]
        report = audit_layers(pages, ocr)
        self.assertEqual(report["ocr_model_numeric_pages_compared"], 2)
        self.assertEqual(report["ocr_model_numeric_disagreement_pages"], 1)
        self.assertEqual(report["ocr_model_numeric_disagreements"][0]["ocr_only_sample"], ["44"])
        self.assertEqual(report["ocr_model_numeric_disagreements"][0]["model_only_sample"], ["43"])
        self.assertEqual(report["conflicts"], [])

    def test_three_way_numeric_patterns_are_review_labels_not_accuracy_votes(self):
        digest = "a" * 64
        cases = [(1, 1, 1), (1, 2, 1), (1, 1, 2),
                 (1, 2, 2), (1, 2, 3)]
        pages = [
            {"document_sha256": digest, "page_number": index,
             "route": "native", "local_text": f"Amount {local}",
             "local_markdown": f"Amount {local}",
             "adjudicated_markdown": f"Amount {model}"}
            for index, (local, _, model) in enumerate(cases, 1)
        ]
        ocr = [
            {"document_sha256": digest, "page_number": index,
             "text": f"Amount {ocr_number}"}
            for index, (_, ocr_number, _) in enumerate(cases, 1)
        ]
        report = audit_layers(pages, ocr)
        self.assertEqual(report["numeric_triad_pages_compared"], 5)
        self.assertEqual(report["numeric_triad_pattern_counts"], {
            "all_equal": 1, "local_model_equal": 1, "local_ocr_equal": 1,
            "model_ocr_equal": 1, "all_different": 1,
        })
        self.assertEqual(len(report["numeric_triad_disagreements"]), 4)
        self.assertEqual(report["numeric_triad_disagreements"][-1]["pattern"],
                         "all_different")
        self.assertEqual(report["numeric_triad_disagreements"][-1]["route"],
                         "native")

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
