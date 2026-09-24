from __future__ import annotations

import unittest
import json
import tempfile
from pathlib import Path

from PIL import Image

from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.db import json_text
from sansad_pipeline.storage import Store, now
from scripts.audit_ocr_layout import (
    numeric_difference, page_rows, sample_rows, separator_rows, similarity,
)


class LayoutAuditTests(unittest.TestCase):
    def test_separator_rows_do_not_count_prose_or_single_hyphens(self):
        markdown = "Prose with a hyphen\n| A | B |\n|---|:---:|\n| text | more |\n---"
        self.assertEqual(separator_rows(markdown), 1)

    def test_sample_prefers_suspect_pages_without_losing_ordinary_pages(self):
        rows = [
            {"document_sha256": f"{number:064x}", "page_number": 1,
             "separator_rows": 4 if number < 5 else 0}
            for number in range(8)
        ]
        selected = sample_rows(rows, 7, 17)
        self.assertEqual(len(selected), 7)
        self.assertEqual(len({row["document_sha256"] for row in selected}), 7)
        self.assertTrue(any(row["separator_rows"] == 0 for row in selected))
        self.assertEqual(selected, sample_rows(rows, 7, 17))
        self.assertEqual(len(sample_rows(rows, 20, 17)), 8)
        with self.assertRaises(ValueError):
            sample_rows(rows, 0, 17)

    def test_sample_includes_numeric_disagreements_and_random_baseline(self):
        rows = [
            {"document_sha256": f"{number:064x}", "page_number": 1,
             "separator_rows": 4 if number < 4 else 0,
             "model_numeric_disagreement": 4 <= number < 8}
            for number in range(12)
        ]
        selected = sample_rows(rows, 9, 17)
        self.assertEqual(len(selected), 9)
        self.assertEqual(len({row["document_sha256"] for row in selected}), 9)
        self.assertEqual(
            {reason: sum(row["selection_stratum"] == reason for row in selected)
             for reason in ("layout", "numeric", "random")},
            {"layout": 3, "numeric": 3, "random": 3},
        )
        self.assertEqual(len(sample_rows(rows, 1, 17)), 1)

    def test_sample_includes_both_native_and_ocr_routes(self):
        rows = [
            {"document_sha256": f"{number:064x}", "page_number": 1,
             "route": "ocr" if number == 0 else "native", "separator_rows": 0}
            for number in range(20)
        ]
        selected = sample_rows(rows, 12, 17)
        self.assertEqual({row["route"] for row in selected}, {"ocr", "native"})
        self.assertEqual(sum(row["selection_stratum"] == "route_baseline"
                             for row in selected), 2)
        self.assertEqual(len(selected), 12)

    def test_similarity_is_format_tolerant_but_not_a_truth_claim(self):
        self.assertEqual(similarity("# Question", "Question"), 1.0)
        self.assertLess(similarity("Government are not doing it", "Government are doing it"), 1.0)

    def test_numeric_difference_keeps_both_directions(self):
        self.assertEqual(
            numeric_difference("42 and 42", "42 and 4.2"),
            {"candidate_only": ["42"], "tesseract_only": ["4.2"]},
        )

    def test_page_rows_selects_scoped_latest_artifacts_across_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            Image.new("RGB", (50, 50), "white").save(source, "PDF")
            store = Store(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            item = store.ingest(source)
            self.assertIsNotNone(store.db.one(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_adjudications_page_provider'"
            ))
            artifact = root / "page.json"
            artifact.write_text(json.dumps({"markdown": "| A | B |\n|---|---|"}), encoding="utf-8")
            run = store.db.execute(
                """INSERT INTO runs(document_sha256,status,config_json,artifact_dir,started_at)
                   VALUES (?,'complete','{}',?,?)""",
                (item.sha256, str(root), now()),
            )
            store.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (run, 1, "ocr", "liteparse", 10, None, "review", "[]", str(artifact)),
            )
            store.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (run, 2, "native", "liteparse", 10, None, "review", "[]", str(artifact)),
            )
            store.db.execute(
                """INSERT INTO census_records
                   (record_id,source_type,house,parliament_number,session,
                    title,language,source_url,official_page_url,api_url,
                    api_params_json,raw_json,discovered_at,
                    acquisition_status,document_sha256)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("one", "questions_answers", "lok_sabha", "01", "I", "Test", "English",
                 "", "", "", "{}", json_text({}), now(), "downloaded", item.sha256),
            )
            rows = page_rows(store, "lok_sabha", "01", "I")
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["separator_rows"], 1)
            self.assertEqual([row["route"] for row in rows], ["ocr", "native"])
            self.assertFalse(rows[0]["model_numeric_disagreement"])
            self.assertEqual(rows[0]["document_sha256"], item.sha256)
            self.assertEqual(page_rows(store, "lok_sabha", "01", "II"), [])
            model_dir = root / "model"
            model_dir.mkdir()
            (model_dir / "adjudicated.md").write_text("Question 42", encoding="utf-8")
            store.db.execute(
                """INSERT INTO adjudications
                   (run_id,page_number,provider,model,request_sha256,response_path,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (run, 1, "openrouter", "stealth/test", "abc",
                 str(model_dir / "response.json"), now()),
            )
            plan = store.db.all(
                """EXPLAIN QUERY PLAN SELECT response_path FROM adjudications
                   WHERE run_id=? AND page_number=? AND provider='openrouter'
                   ORDER BY id DESC LIMIT 1""", (run, 1),
            )
            self.assertTrue(any("idx_adjudications_page_provider" in row["detail"]
                                for row in plan))
            rows = page_rows(store, "lok_sabha", "01", "I")
            self.assertTrue(rows[0]["model_numeric_disagreement"])
            self.assertEqual(rows[0]["model_markdown"], "Question 42")
            (model_dir / "adjudicated.md").write_text(
                "| A | B |\n|---|---|\nPage 1 of 1", encoding="utf-8"
            )
            rows = page_rows(store, "lok_sabha", "01", "I")
            self.assertFalse(rows[0]["model_numeric_disagreement"])
            self.assertTrue(rows[0]["model_raw_numeric_disagreement"])


if __name__ == "__main__":
    unittest.main()
