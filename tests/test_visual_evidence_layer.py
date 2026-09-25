from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts.full_ocr_layer import Page, inventory_sha256
from scripts.visual_evidence_layer import (METHOD, completed_shards, page_row,
                                           publish_shard, shard_paths, verify_rows)


class VisualEvidenceLayerTests(unittest.TestCase):
    def test_script_entry_point_imports_from_outside_checkout(self):
        script = Path(__file__).resolve().parents[1] / "scripts/visual_evidence_layer.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(script), "--help"],
                                    cwd=directory, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rows_require_exact_page_identity_and_consistent_pixels(self):
        pages = [Page("a" * 64, 1, Path("a.pdf")), Page("b" * 64, 2, Path("b.pdf"))]
        rows = [{"document_sha256": page.document_sha256,
                 "page_number": page.page_number, "method": METHOD, "dpi": 250,
                 "image_pixels": 10000, "image_dark_pixels": dark,
                 "image_dark_pixel_cutoff": 250, "visually_blank": dark <= 25}
                for page, dark in zip(pages, (0, 100), strict=True)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "part.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            self.assertEqual(verify_rows(path, pages, 0, 1, 250), {
                "visually_blank": 1, "visibly_nonblank": 1,
            })
            rows[1]["visually_blank"] = True
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row) + "\n")
            with self.assertRaisesRegex(RuntimeError, "invalid page"):
                verify_rows(path, pages, 0, 1, 250)

    def test_resume_verifies_shard_checksum_and_contiguous_inventory(self):
        pages = [Page("a" * 64, 1, Path("a.pdf"))]
        inventory = inventory_sha256(pages)
        root = "layers/visual-evidence/test/inventory-" + inventory[:16] + "/render-250dpi-v1"
        archive_path, manifest_path = shard_paths(root, 0, 0)
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "part.jsonl.gz"
            manifest_file = Path(directory) / "part.json"
            with gzip.open(archive, "wt", encoding="utf-8") as handle:
                handle.write(json.dumps({
                    "document_sha256": pages[0].document_sha256,
                    "page_number": 1, "method": METHOD, "dpi": 250,
                    "image_pixels": 10000, "image_dark_pixels": 0,
                    "image_dark_pixel_cutoff": 250, "visually_blank": True,
                }) + "\n")
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = {"path": archive_path, "start_index": 0, "end_index": 0,
                        "first_key": pages[0].key, "last_key": pages[0].key,
                        "record_count": 1, "inventory_sha256": inventory,
                        "method": METHOD, "dpi": 250,
                        "visual_counts": {"visually_blank": 1, "visibly_nonblank": 0},
                        "sha256": digest, "bytes": archive.stat().st_size}
            manifest_file.write_text(json.dumps(manifest))
            api = mock.Mock()
            api.list_repo_files.return_value = [archive_path, manifest_path]
            api.get_paths_info.return_value = [SimpleNamespace(
                size=archive.stat().st_size, lfs=SimpleNamespace(sha256=digest),
            )]
            with mock.patch("scripts.visual_evidence_layer.hf_hub_download",
                            side_effect=lambda repo, path, **kwargs:
                            str(archive if path == archive_path else manifest_file)):
                self.assertEqual(len(completed_shards(api, "repo", root, pages,
                                                      inventory, 250, "token")), 1)
                manifest["visual_counts"]["visually_blank"] = 0
                manifest_file.write_text(json.dumps(manifest))
                with self.assertRaisesRegex(RuntimeError, "counts mismatch"):
                    completed_shards(api, "repo", root, pages, inventory, 250, "token")

    def test_page_row_uses_rendered_original_and_rejects_bad_metrics(self):
        page = Page("a" * 64, 3, Path("source.pdf"))
        metrics = {"image_pixels": 10000, "image_dark_pixels": 0,
                   "image_dark_pixel_cutoff": 250, "visually_blank": True}
        with mock.patch("scripts.visual_evidence_layer.render_page",
                        return_value=Path("unused.png")) as render, \
             mock.patch("scripts.visual_evidence_layer.rendered_ink_metrics",
                        return_value=metrics):
            row = page_row(page, 250)
        self.assertEqual(row["document_sha256"], page.document_sha256)
        self.assertTrue(row["visually_blank"])
        self.assertEqual(render.call_args.args[0:2], (page.raw_path, 3))
        with mock.patch("scripts.visual_evidence_layer.render_page",
                        return_value=Path("unused.png")), \
             mock.patch("scripts.visual_evidence_layer.rendered_ink_metrics",
                        return_value={**metrics, "image_dark_pixels": 100}):
            with self.assertRaisesRegex(RuntimeError, "invalid rendered image"):
                page_row(page, 250)

    def test_failed_page_render_does_not_publish_partial_shard(self):
        page = Page("a" * 64, 1, Path("source.pdf"))
        with mock.patch("scripts.visual_evidence_layer.page_row",
                        side_effect=RuntimeError("render timed out")), \
             mock.patch("scripts.visual_evidence_layer._commit_with_retry") as commit:
            with self.assertRaisesRegex(RuntimeError, "render timed out"):
                publish_shard(mock.Mock(), "repo", "layers/test", [(0, page)],
                              inventory_sha256([page]), 250, "token")
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
