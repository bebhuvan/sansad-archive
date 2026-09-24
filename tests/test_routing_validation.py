from __future__ import annotations

import unittest

from sansad_pipeline.config import RoutingConfig, ValidationConfig
from sansad_pipeline.liteparse_engine import ExtractedPage
from sansad_pipeline.routing import decide
from sansad_pipeline.validation import validate


def page(
    text: str,
    reasons: list[str] | None = None,
    markdown: str | None = None,
    full_page_image: bool = False,
):
    return ExtractedPage(
        page_number=1,
        text=text,
        markdown=markdown if markdown is not None else text,
        width=100,
        height=100,
        complexity={"reasons": reasons or [], "full_page_image": full_page_image},
        mean_confidence=None,
        text_items=1,
        vector_lines=0,
        vector_shapes=0,
    )


class RoutingTests(unittest.TestCase):
    def test_sparse_native_table_does_not_force_ocr(self):
        decision = decide(
            page("A useful native table with several rows containing 123 and 14.2", ["sparse-text"]),
            RoutingConfig(),
        )
        self.assertEqual(decision.route, "native")

    def test_empty_and_scanned_force_ocr(self):
        decision = decide(page("", ["scanned"]), RoutingConfig())
        self.assertEqual(decision.route, "ocr")
        self.assertIn("empty-native-text", decision.reasons)

    def test_full_page_image_forces_fresh_ocr_even_with_existing_text_layer(self):
        decision = decide(
            page("A long but pre-existing government OCR text layer", full_page_image=True),
            RoutingConfig(),
        )
        self.assertEqual(decision.route, "ocr")
        self.assertIn("full-page-image", decision.reasons)


class ValidationTests(unittest.TestCase):
    def test_numeric_disagreement_is_reviewed(self):
        native = page("value 14.2")
        ocr = page("value 142")
        result = validate(ocr, route="ocr", native=native, config=ValidationConfig())
        self.assertEqual(result.status, "review")
        self.assertIn("native-ocr-numeric-disagreement", result.flags)

    def test_consistent_table_is_accepted(self):
        candidate = page("a 1 b 2", markdown="| A | B |\n|---|---|\n| 1 | 2 |")
        result = validate(candidate, route="native", native=candidate, config=ValidationConfig())
        self.assertEqual(result.status, "accepted")


if __name__ == "__main__":
    unittest.main()
