from __future__ import annotations

import unittest
import tempfile
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from sansad_pipeline.config import Config
from sansad_pipeline.image_quality import rendered_ink_metrics
from sansad_pipeline.liteparse_engine import LiteParseEngine
from sansad_pipeline.openrouter import render_page


def _parse_in_spawned_worker(path: str) -> tuple[int, bool]:
    pages = LiteParseEngine(Config(project_root=Path.cwd())).extract(Path(path), ocr=True)
    return len(pages), bool(pages[0].text.strip())


class LiteParseCompletenessTests(unittest.TestCase):
    def test_missing_parser_page_rejects_document(self):
        page = SimpleNamespace(
            page_num=1, text="First page", markdown="First page",
            width=100, height=100, complexity=None, text_items=[],
            vector_graphics=None,
        )
        parser = Mock()
        parser.parse.return_value = SimpleNamespace(pages=[page], total_pages=2)
        with patch("sansad_pipeline.liteparse_engine.LiteParse", return_value=parser) as factory:
            with self.assertRaisesRegex(RuntimeError, "expected .*2 total"):
                LiteParseEngine(Config(project_root=Path.cwd())).extract(
                    Path("unused.pdf"), ocr=False
                )
        self.assertEqual(factory.call_args.kwargs["pool_size"], 1)
        self.assertEqual(factory.call_args.kwargs["parse_timeout"], 300)
        parser.close.assert_called_once()

    def test_hard_timeout_pool_works_inside_cloud_style_process_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "page.pdf"
            image = Image.new("RGB", (1200, 1600), "white")
            ImageDraw.Draw(image).text((80, 80), "QUESTION 42: Test record", fill="black")
            image.save(pdf, "PDF")
            with ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn")) as pool:
                self.assertEqual(pool.submit(_parse_in_spawned_worker, str(pdf)).result(timeout=30),
                                 (1, True))

    def test_real_rasterized_ocr_exposes_page_ink_metrics(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "page.pdf"
            image = Image.new("RGB", (600, 800), "white")
            ImageDraw.Draw(image).text((40, 40), "QUESTION 42: Test record", fill="black")
            image.save(pdf, "PDF")
            page = LiteParseEngine(Config(project_root=Path.cwd())).extract(
                pdf, ocr=True, target_pages=[1], rasterize=True,
            )[0]
            self.assertTrue(page.text.strip())
            self.assertFalse(page.visual_quality["visually_blank"])
            self.assertGreater(page.visual_quality["image_dark_pixels"], 25)
            screenshot = render_page(pdf, 1, Path(directory) / "render", 250)
            self.assertEqual(page.visual_quality, rendered_ink_metrics(screenshot))

    def test_parser_worker_closes_after_parse_failure(self):
        parser = Mock()
        parser.parse.side_effect = RuntimeError("parser failed")
        with patch("sansad_pipeline.liteparse_engine.LiteParse", return_value=parser):
            with self.assertRaisesRegex(RuntimeError, "parser failed"):
                LiteParseEngine(Config(project_root=Path.cwd())).extract(
                    Path("unused.pdf"), ocr=False
                )
        parser.close.assert_called_once()

    def test_rasterized_ocr_retains_original_page_visual_metrics(self):
        image = Image.new("RGB", (100, 100), "white")
        page = SimpleNamespace(
            page_num=1, text="Invented text", markdown="Invented text",
            width=100, height=100, complexity=None, text_items=[],
            vector_graphics=None,
        )
        parser = Mock()
        parser.parse.return_value = SimpleNamespace(pages=[page], total_pages=1)
        with tempfile.TemporaryDirectory() as directory:
            picture = Path(directory) / "blank.png"
            image.save(picture)
            with patch("sansad_pipeline.liteparse_engine.render_page",
                       return_value=picture) as render, \
                 patch("sansad_pipeline.liteparse_engine.LiteParse", return_value=parser):
                result = LiteParseEngine(Config(project_root=Path.cwd())).extract(
                    Path("unused.pdf"), ocr=True, target_pages=[2], rasterize=True,
                )
            self.assertEqual(render.call_args.args[1], 2)
        self.assertEqual(result[0].page_number, 2)
        self.assertTrue(result[0].visual_quality["visually_blank"])
        self.assertEqual(result[0].visual_quality["image_dark_pixels"], 0)
        parser.close.assert_called_once()

    def test_rasterized_ocr_stops_before_parse_on_render_timeout(self):
        with patch("sansad_pipeline.liteparse_engine.render_page",
                   side_effect=RuntimeError("LiteParse screenshot timed out for page 2")), \
             patch("sansad_pipeline.liteparse_engine.LiteParse") as parser:
            with self.assertRaisesRegex(RuntimeError, "timed out for page 2"):
                LiteParseEngine(Config(project_root=Path.cwd())).extract(
                    Path("unused.pdf"), ocr=True, target_pages=[2], rasterize=True,
                )
        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
