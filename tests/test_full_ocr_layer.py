from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from scripts.full_ocr_layer import Page, inventory_sha256, ocr_rows, verify_shard_rows


class FakeEngine:
    version = "2.14.7"

    def __init__(self):
        self.calls = []

    def extract(self, pdf, *, ocr, target_pages, rasterize):
        self.calls.append((pdf, ocr, target_pages, rasterize))
        return [type("Extracted", (), {
            "page_number": number, "text": f"OCR page {number}",
            "markdown": f"OCR page {number}", "mean_confidence": 0.9,
        })() for number in target_pages]


class FullOcrLayerTests(unittest.TestCase):
    def test_ocr_keeps_original_pdf_boundaries_and_every_page(self):
        a, b = "a" * 64, "b" * 64
        pages = [Page(a, number, Path("a.pdf")) for number in range(1, 4)]
        pages += [Page(b, number, Path("b.pdf")) for number in range(1, 9)]
        engine = FakeEngine()
        rows = ocr_rows(engine, pages)
        self.assertEqual(len(rows), len(pages))
        self.assertEqual([(row["document_sha256"], row["page_number"]) for row in rows],
                         [(page.document_sha256, page.page_number) for page in pages])
        self.assertTrue(all(call[1] and call[3] for call in engine.calls))
        self.assertEqual([call[2] for call in engine.calls], [[1, 2, 3], [1, 2, 3, 4, 5],
                                                            [6, 7, 8]])

    def test_shard_verifier_detects_missing_or_wrong_page(self):
        pages = [Page("a" * 64, number, Path("a.pdf")) for number in (1, 2)]
        rows = ocr_rows(FakeEngine(), pages)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            verify_shard_rows(path, pages, 0, 1, "2.14.7")
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps(rows[1]) + "\n")
            with self.assertRaisesRegex(RuntimeError, "invalid page"):
                verify_shard_rows(path, pages, 0, 1, "2.14.7")

    def test_inventory_digest_changes_with_page_identity(self):
        first = [Page("a" * 64, 1, Path("a.pdf"))]
        second = [Page("a" * 64, 2, Path("a.pdf"))]
        self.assertNotEqual(inventory_sha256(first), inventory_sha256(second))


if __name__ == "__main__":
    unittest.main()
