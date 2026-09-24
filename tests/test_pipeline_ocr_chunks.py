from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.liteparse_engine import ExtractedPage
from sansad_pipeline.pipeline import Pipeline


class FakeEngine:
    name = "fake"
    version = "1"

    def __init__(self):
        self.ocr_batches = []

    def extract(self, pdf, *, ocr, target_pages=None, rasterize=False):
        if ocr:
            self.ocr_batches.append((list(target_pages), rasterize))
        return [
            ExtractedPage(
                page_number=n, text=f"Page {n}" if ocr else "",
                markdown=f"Page {n}" if ocr else "", width=100, height=100,
                complexity={} if ocr else {"full_page_image": True},
                mean_confidence=0.9 if ocr else None, text_items=1 if ocr else 0,
                vector_lines=0, vector_shapes=0,
            )
            for n in (target_pages if ocr else range(1, 34))
        ]


class PipelineOcrChunkTests(unittest.TestCase):
    def test_full_page_scans_are_rasterized_in_bounded_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf = root / "source.pdf"
            pdf.write_bytes(b"%PDF-test")
            pipeline = Pipeline(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            engine = FakeEngine()
            pipeline.engine = engine
            document = pipeline.store.ingest(pdf)
            pipeline.process(document.sha256)
            self.assertEqual([len(batch) for batch, _ in engine.ocr_batches], [16, 16, 1])
            self.assertTrue(all(rasterize for _, rasterize in engine.ocr_batches))


if __name__ == "__main__":
    unittest.main()
