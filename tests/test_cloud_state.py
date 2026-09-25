from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import cloud_state


class CloudStateTests(unittest.TestCase):
    def test_save_log_summary_excludes_large_raw_index(self):
        manifest = {
            "version": 3, "created_at": "now", "files": 10,
            "archive_bytes": 1234, "raw_shards": [{"path": "raw.tar.zst"}],
            "raw_index": {f"file-{number}.pdf": {"sha256": "x" * 64}
                          for number in range(1000)},
            "commit_url": "https://example.test/commit",
        }
        summary = cloud_state.save_log_summary(manifest)
        self.assertEqual(summary["raw_pdfs"], 1000)
        self.assertEqual(summary["raw_shards"], 1)
        self.assertNotIn("raw_index", summary)
        self.assertLess(len(json.dumps(summary)), 300)

    def test_v2_raw_archive_migrates_to_incremental_shards_without_reupload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data/raw"
            raw.mkdir(parents=True)
            old = raw / "old.pdf"
            old.write_bytes(b"%PDF-old")
            hub = root / "hub"
            base = hub / "state/checkpoints/test"
            base.mkdir(parents=True)
            raw_info = cloud_state._write_archive(base / "raw-old.tar.zst", [
                (old, "data/raw/old.pdf")
            ])
            raw_info["path"] = "state/checkpoints/test/raw-old.tar.zst"
            raw_info["inventory_sha256"] = cloud_state._raw_inventory(
                cloud_state._raw_index([(old, "data/raw/old.pdf")])
            )
            state_info = cloud_state._write_archive(base / "state.tar.zst", [])
            state_info["path"] = "state/checkpoints/test/state.tar.zst"
            (base / "checkpoint.json").write_text(json.dumps({
                "version": 2, "raw": raw_info, "state": state_info,
            }))

            api = Mock()
            def commit(**kwargs):
                for operation in kwargs["operations"]:
                    target = hub / operation.path_in_repo
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = operation.path_or_fileobj
                    target.write_bytes(Path(source).read_bytes() if isinstance(source, str) else source)
                return SimpleNamespace(commit_url="https://example.test/commit")
            api.create_commit.side_effect = commit
            def download(*, filename, **kwargs):
                return str(hub / filename)
            with patch.object(cloud_state, "PROJECT_ROOT", root), \
                 patch.object(cloud_state, "DATA", root / "data"), \
                 patch("huggingface_hub.HfApi", return_value=api), \
                 patch("huggingface_hub.hf_hub_download", side_effect=download):
                migrated = cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual(migrated["version"], 3)
                self.assertEqual(len(migrated["raw_shards"]), 1)
                self.assertEqual(len(api.create_commit.call_args.kwargs["operations"]), 2)
                (raw / "new.pdf").write_bytes(b"%PDF-new")
                updated = cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual([item["files"] for item in updated["raw_shards"]], [1, 1])
                shutil.rmtree(raw)
                cloud_state.restore("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual((raw / "old.pdf").read_bytes(), b"%PDF-old")
                self.assertEqual((raw / "new.pdf").read_bytes(), b"%PDF-new")

    def test_raw_pdf_archive_is_not_reuploaded_when_only_state_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data" / "raw"
            raw.mkdir(parents=True)
            (raw / "example.pdf").write_bytes(b"%PDF-test")
            hub = root / "hub"
            hub.mkdir()
            api = Mock()
            api.create_commit.return_value = SimpleNamespace(commit_url="https://example.test/commit")
            def commit(**kwargs):
                for operation in kwargs["operations"]:
                    target = hub / operation.path_in_repo
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = operation.path_or_fileobj
                    target.write_bytes(Path(source).read_bytes() if isinstance(source, str) else source)
                return SimpleNamespace(commit_url="https://example.test/commit")
            api.create_commit.side_effect = commit
            def download(*, filename, **kwargs):
                path = hub / filename
                if not path.is_file():
                    error = RuntimeError("missing")
                    error.response = SimpleNamespace(status_code=404)
                    raise error
                return str(path)
            with patch.object(cloud_state, "PROJECT_ROOT", root), \
                 patch.object(cloud_state, "DATA", root / "data"), \
                 patch("huggingface_hub.HfApi", return_value=api), \
                 patch("huggingface_hub.hf_hub_download", side_effect=download):
                result = cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual(result["version"], 3)
                self.assertEqual(len(api.create_commit.call_args.kwargs["operations"]), 3)
                manifest = json.loads((hub / "state/checkpoints/test/checkpoint.json").read_text())
                self.assertEqual(len(manifest["raw_shards"]), 1)
                self.assertEqual(len(manifest["raw_shards"][0]["sha256"]), 64)
                self.assertEqual(result["files"], 1)

                logs = root / "data" / "logs"
                logs.mkdir()
                (logs / "progress.jsonl").write_text("progress", encoding="utf-8")
                cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
                paths = [op.path_in_repo for op in api.create_commit.call_args.kwargs["operations"]]
                self.assertEqual(len(paths), 2)
                self.assertFalse(any("/raw/" in path for path in paths))

                shutil.rmtree(raw)
                shutil.rmtree(logs)
                restored = cloud_state.restore("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual(restored["version"], 3)
                self.assertEqual((raw / "example.pdf").read_bytes(), b"%PDF-test")
                self.assertEqual((logs / "progress.jsonl").read_text(), "progress")

                (raw / "new.pdf").write_bytes(b"%PDF-new")
                cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
                paths = [op.path_in_repo for op in api.create_commit.call_args.kwargs["operations"]]
                self.assertEqual(len(paths), 3)
                self.assertTrue(any("/raw/" in path for path in paths))
                manifest = json.loads((hub / "state/checkpoints/test/checkpoint.json").read_text())
                self.assertEqual(len(manifest["raw_shards"]), 2)
                self.assertEqual([shard["files"] for shard in manifest["raw_shards"]], [1, 1])
                shutil.rmtree(raw)
                cloud_state.restore("user/dataset", "state/checkpoints/test", token="test")
                self.assertEqual((raw / "example.pdf").read_bytes(), b"%PDF-test")
                self.assertEqual((raw / "new.pdf").read_bytes(), b"%PDF-new")

    def test_restore_only_treats_missing_checkpoint_as_empty(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(cloud_state, "PROJECT_ROOT", root), \
                 patch.object(cloud_state, "DATA", root / "data"), \
                 patch("huggingface_hub.hf_hub_download") as download:
                for status, should_raise in ((404, False), (429, True)):
                    error = RuntimeError("Hub failure")
                    error.response = SimpleNamespace(status_code=status)
                    download.side_effect = error
                    if should_raise:
                        with self.assertRaises(RuntimeError):
                            cloud_state.restore("user/dataset", "state/checkpoints/test", token="test")
                    else:
                        self.assertFalse(cloud_state.restore(
                            "user/dataset", "state/checkpoints/test", token="test"
                        )["restored"])

    def test_restores_legacy_single_archive_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data" / "raw"
            raw.mkdir(parents=True)
            source = raw / "old.pdf"
            source.write_bytes(b"%PDF-legacy")
            hub = root / "hub"
            hub.mkdir()
            archive = hub / "checkpoint.tar.zst"
            info = cloud_state._write_archive(archive, [(source, "data/raw/old.pdf")])
            manifest = hub / "checkpoint.json"
            manifest.write_text(json.dumps({
                "archive_bytes": info["bytes"], "archive_sha256": info["sha256"],
                "path_in_repo": "state/checkpoints/test/checkpoint.tar.zst",
            }))
            source.unlink()
            def download(*, filename, **kwargs):
                return str(manifest if filename.endswith("checkpoint.json") else archive)
            with patch.object(cloud_state, "PROJECT_ROOT", root), \
                 patch.object(cloud_state, "DATA", root / "data"), \
                 patch("huggingface_hub.hf_hub_download", side_effect=download):
                self.assertEqual(
                    cloud_state.restore("user/dataset", "state/checkpoints/test", token="test")["version"],
                    1,
                )
            self.assertEqual(source.read_bytes(), b"%PDF-legacy")


if __name__ == "__main__":
    unittest.main()
