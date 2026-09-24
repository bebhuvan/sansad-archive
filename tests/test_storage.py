from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.storage import Store


class StorageTests(unittest.TestCase):
    def test_existing_raw_pdf_must_match_its_content_address(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            Image.new("RGB", (20, 20), "white").save(source, "PDF")
            store = Store(Config(project_root=root, storage=StorageConfig(root=Path("data"))))
            first = store.ingest(source, source_uri="https://example.test/first.pdf")
            first.raw_path.chmod(0o644)
            first.raw_path.write_bytes(b"%PDF-corrupted")

            with self.assertRaisesRegex(IOError, "stored PDF SHA-256 mismatch"):
                store.ingest(source, source_uri="https://example.test/second.pdf")
            self.assertEqual(store.db.one("SELECT COUNT(*) n FROM sources")["n"], 1)

    def test_concurrent_identical_ingest_uses_one_immutable_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.pdf"
            Image.new("RGB", (20, 20), "white").save(source, "PDF")
            config = Config(project_root=root, storage=StorageConfig(root=Path("data")))

            def ingest():
                return Store(config).ingest(source, source_uri="https://example.test/same.pdf")

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: ingest(), range(2)))

            self.assertEqual(results[0].sha256, results[1].sha256)
            self.assertTrue(results[0].raw_path.is_file())
            store = Store(config)
            self.assertEqual(store.db.one("SELECT COUNT(*) n FROM documents")["n"], 1)


if __name__ == "__main__":
    unittest.main()
