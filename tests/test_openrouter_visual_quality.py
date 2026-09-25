from __future__ import annotations

import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sansad_pipeline.config import Config, OpenRouterConfig, StorageConfig
from sansad_pipeline.openrouter import (BASE_PROMPT, PROMPT, OpenRouterAdjudicator,
                                        local_candidate,
                                        visual_quality_flags)


class OpenRouterVisualQualityTests(unittest.TestCase):
    def test_both_prompts_treat_blank_as_empty_not_illegible(self):
        for prompt in (BASE_PROMPT, PROMPT):
            self.assertIn("If the page is blank, return an empty response", prompt)
            self.assertIn("do not write [ILLEGIBLE]", prompt)

    def test_adjudication_persists_blank_image_flag_without_editing_model_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "blank.pdf"
            Image.new("RGB", (100, 100), "white").save(source, "PDF")
            config = Config(
                project_root=root, storage=StorageConfig(root=Path("data")),
                openrouter=OpenRouterConfig(enabled=True, model="free/test"),
            )
            worker = OpenRouterAdjudicator(config)
            document = worker.store.ingest(source)
            artifact_dir = root / "page-artifacts"
            artifact_dir.mkdir()
            page_artifact = artifact_dir / "page-00001.json"
            page_artifact.write_text(json.dumps({
                "text": "", "markdown": "```text\n\n```",
            }))
            run_id = worker.store.db.execute(
                """INSERT INTO runs(document_sha256,status,config_json,artifact_dir,started_at)
                   VALUES (?,'complete','{}',?,'now')""",
                (document.sha256, str(artifact_dir)),
            )
            worker.store.db.execute(
                "INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, 1, "ocr", "liteparse", 0, None, "review", '["empty-text"]',
                 str(page_artifact)),
            )
            image = root / "blank.png"
            Image.new("RGB", (100, 100), "white").save(image)
            request_payloads = []

            def fake_post(request, log_path):
                request_payloads.append(json.loads(request.data))
                return ({"choices": [{"message": {"content": "Invented words"},
                                       "finish_reason": "stop"}],
                         "usage": {"cost": 0}}, {})

            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                 patch.object(worker, "_model_snapshot", return_value={"id": "free/test"}), \
                 patch.object(worker, "_render_page", return_value=image), \
                 patch.object(worker, "_post", side_effect=fake_post):
                self.assertEqual(worker.adjudicate(document.sha256, pages=[1]), [1])
            prompt = request_payloads[0]["messages"][0]["content"][0]["text"]
            self.assertNotIn("LOCAL CANDIDATE", prompt)
            provenance = next(artifact_dir.rglob("provenance.json"))
            saved = json.loads(provenance.read_text())
            self.assertTrue(saved["visual_quality"]["visually_blank"])
            self.assertEqual(saved["quality_flags"],
                             ["model-nonempty-on-visually-blank-page"])
            self.assertEqual(provenance.with_name("adjudicated.md").read_text(),
                             "Invented words")
            Image.new("RGB", (100, 100), "white").save(image)
            empty_reply = ({"choices": [{"message": {"content": ""},
                                          "finish_reason": "stop"}],
                            "usage": {"cost": 0}}, {})
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                 patch.object(worker, "_model_snapshot", return_value={"id": "free/test"}), \
                 patch.object(worker, "_render_page", return_value=image), \
                 patch.object(worker, "_post", return_value=empty_reply):
                self.assertEqual(worker.adjudicate(document.sha256, pages=[1]), [1])
            saved = [json.loads(path.read_text()) for path in artifact_dir.rglob("provenance.json")]
            self.assertEqual(sum(item["blank_response_verified"] for item in saved), 1)
            self.assertEqual(sum(not path.read_text().strip()
                                 for path in artifact_dir.rglob("adjudicated.md")), 1)
            self.assertEqual(worker.store.db.one(
                "SELECT COUNT(*) AS n FROM adjudications WHERE run_id=?", (run_id,))["n"], 2)
            Image.new("RGB", (100, 100), "white").save(image)
            with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only-key"}), \
                 patch.object(worker, "_model_snapshot", return_value={"id": "free/test"}), \
                 patch.object(worker, "_render_page", return_value=image), \
                 patch.object(worker, "_post", return_value=empty_reply), \
                 patch("sansad_pipeline.openrouter.rendered_ink_metrics", return_value={
                     "image_pixels": 10000, "image_dark_pixels": 100,
                     "image_dark_pixel_cutoff": 250, "visually_blank": False,
                 }):
                with self.assertRaisesRegex(RuntimeError, "empty transcription"):
                    worker.adjudicate(document.sha256, pages=[1])

    def test_empty_liteparse_wrapper_is_not_sent_as_candidate(self):
        self.assertEqual(local_candidate({"text": "", "markdown": "```text\n\n```"}), "")
        self.assertEqual(local_candidate({"text": "Question", "markdown": "# Question"}),
                         "# Question")
        self.assertEqual(local_candidate({"text": "", "markdown": "| A | B |"}),
                         "| A | B |")

    def test_blank_page_flags_are_separate_from_the_raw_model_transcript(self):
        blank = {"visually_blank": True, "image_pixels": 10000,
                 "image_dark_pixels": 0}
        local = {"text": "", "markdown": "```text\n\n```"}
        self.assertEqual(visual_quality_flags(blank, local, "Y 1300 ."),
                         ["model-nonempty-on-visually-blank-page"])
        self.assertEqual(visual_quality_flags(blank, local, ""), [])
        self.assertEqual(visual_quality_flags(blank,
                                              {"text": "Ghost", "markdown": "Ghost"},
                                              "Ghost"),
                         ["local-nonempty-on-visually-blank-page",
                          "model-nonempty-on-visually-blank-page"])
        self.assertEqual(visual_quality_flags({"visually_blank": False}, local,
                                              "Visible document"), [])


if __name__ == "__main__":
    unittest.main()
