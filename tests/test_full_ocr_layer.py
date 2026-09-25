from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from sansad_pipeline.config import LiteParseConfig
from scripts.full_ocr_layer import Page, inventory_sha256, ocr_rows, verify_shard_rows
from scripts.plan_full_ocr import plan


class FakeEngine:
    version = "2.14.7"
    name = "liteparse"

    def __init__(self):
        self.calls = []
        self.config = type("Config", (), {"liteparse": LiteParseConfig()})()

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
        self.assertEqual([call[2] for call in engine.calls], [[1, 2, 3], list(range(1, 9))])

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

    def test_verified_selected_image_ocr_is_reused_but_native_and_other_ocr_are_fresh(self):
        digest = "a" * 64
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "page.json"
            artifact_path.write_text(json.dumps({
                "document_sha256": digest, "page_number": 1, "route": "ocr",
                "route_reasons": ["full-page-image"], "engine": "liteparse",
                "engine_version": engine.version, "text": "Saved image OCR",
                "markdown": "Saved image OCR", "mean_confidence": 0.95,
            }))
            config = json.dumps({"liteparse": asdict(engine.config.liteparse)})
            pages = [
                Page(digest, 1, Path("a.pdf"), "ocr", artifact_path, config),
                Page(digest, 2, Path("a.pdf"), "native", artifact_path, config),
                Page(digest, 3, Path("a.pdf")),
            ]
            rows = ocr_rows(engine, pages)
            self.assertEqual(rows[0]["text"], "Saved image OCR")
            self.assertEqual(rows[0]["origin"], "selected-local-rasterized-ocr")
            self.assertEqual(len(rows[0]["source_artifact_sha256"]), 64)
            self.assertEqual([call[2] for call in engine.calls], [[2, 3]])
            self.assertEqual([row["origin"] for row in rows[1:]],
                             ["sidecar-rasterized-ocr"] * 2)

    def test_reuse_fails_closed_on_mismatch_and_reocr_on_config_change(self):
        digest = "a" * 64
        engine = FakeEngine()
        with tempfile.TemporaryDirectory() as directory:
            artifact_path = Path(directory) / "page.json"
            artifact = {
                "document_sha256": digest, "page_number": 2, "route": "ocr",
                "route_reasons": ["full-page-image"], "engine": "liteparse",
                "engine_version": engine.version, "text": "Saved image OCR",
            }
            artifact_path.write_text(json.dumps(artifact))
            config = json.dumps({"liteparse": asdict(engine.config.liteparse)})
            page = Page(digest, 1, Path("a.pdf"), "ocr", artifact_path, config)
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                ocr_rows(engine, [page])
            artifact["page_number"] = 1
            artifact_path.write_text(json.dumps(artifact))
            changed = asdict(engine.config.liteparse)
            changed["full_page_image_dpi"] = 200
            page = Page(digest, 1, Path("a.pdf"), "ocr", artifact_path,
                        json.dumps({"liteparse": changed}))
            self.assertEqual(ocr_rows(engine, [page])[0]["origin"],
                             "sidecar-rasterized-ocr")
            self.assertEqual(len(engine.calls), 1)

    def test_planner_selects_only_completed_scope_without_matching_ocr_marker(self):
        marker_path = "state/snapshot-complete/snapshot-complete-lok_sabha-p01-sII.json"
        complete_path = ("layers/full-ocr/lok_sabha-p01-sII/inventory-abc/"
                         "liteparse-2.14.7-eng-250dpi/complete.json")
        evidence = {"path": marker_path, "sha256": "a" * 64,
                    "checkpoint_path": "checkpoint.json", "raw_inventory_sha256": "b" * 64}
        class FakeApi:
            def list_repo_files(self, repo, *, repo_type):
                return [marker_path, complete_path]

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "marker.json"
            complete = Path(directory) / "complete.json"
            marker.write_text(json.dumps({"scope_status": {"pages": 2}}))
            complete.write_text(json.dumps({
                "completion_marker": evidence, "engine": "liteparse",
                "engine_version": "2.14.7", "source": "elibrary",
                "scope": "lok_sabha-p01-sII", "pages": 2,
            }))
            with (mock.patch("scripts.plan_full_ocr.completion_evidence", return_value=evidence),
                  mock.patch("scripts.plan_full_ocr.hf_hub_download",
                             side_effect=lambda repo, path, **kwargs:
                             str(complete if path == complete_path else marker))):
                self.assertEqual(plan("repo", "token", house="lok_sabha",
                                      max_scopes=2, api=FakeApi()), [])
                complete.write_text(json.dumps({
                    "completion_marker": {**evidence, "sha256": "old"},
                    "engine": "liteparse", "engine_version": "2.14.7",
                    "source": "elibrary", "scope": "lok_sabha-p01-sII", "pages": 2,
                }))
                self.assertEqual(plan("repo", "token", house="lok_sabha",
                                      max_scopes=2, api=FakeApi()), [{
                    "source": "elibrary", "house": "lok_sabha",
                    "parliament": "01", "session": "II",
                }])


if __name__ == "__main__":
    unittest.main()
