import tempfile
import unittest
from pathlib import Path

from scripts.tranche_id import snapshot_tranche


class SnapshotTrancheTests(unittest.TestCase):
    def test_original_pdf_inventory_changes_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "run-1"
            second = root / "run-2"
            first.mkdir()
            second.mkdir()
            for bundle in (first, second):
                (bundle / "pages.jsonl.zst").write_bytes(b"same page text")
                (bundle / "manifest.jsonl").write_bytes(b"selected PDF only")

            original = snapshot_tranche(first, "local")
            self.assertEqual(original, snapshot_tranche(second, "local"))
            self.assertTrue(original.startswith("snapshot-local-"))

            (second / "manifest.jsonl").write_bytes(b"selected PDF plus another original")
            self.assertNotEqual(original, snapshot_tranche(second, "local"))
            self.assertNotEqual(original, snapshot_tranche(first, "model"))


if __name__ == "__main__":
    unittest.main()
