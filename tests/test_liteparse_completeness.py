from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sansad_pipeline.config import Config
from sansad_pipeline.liteparse_engine import LiteParseEngine


class LiteParseCompletenessTests(unittest.TestCase):
    def test_missing_parser_page_rejects_document(self):
        page = SimpleNamespace(
            page_num=1, text="First page", markdown="First page",
            width=100, height=100, complexity=None, text_items=[],
            vector_graphics=None,
        )
        parser = SimpleNamespace(parse=lambda path: SimpleNamespace(
            pages=[page], total_pages=2
        ))
        with patch("sansad_pipeline.liteparse_engine.LiteParse", return_value=parser):
            with self.assertRaisesRegex(RuntimeError, "expected .*2 total"):
                LiteParseEngine(Config(project_root=Path.cwd())).extract(
                    Path("unused.pdf"), ocr=False
                )


if __name__ == "__main__":
    unittest.main()
