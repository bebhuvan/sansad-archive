from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import cloud_state


class CloudStateTests(unittest.TestCase):
    def test_checkpoint_archive_and_manifest_share_one_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "data" / "raw"
            raw.mkdir(parents=True)
            (raw / "example.pdf").write_bytes(b"%PDF-test")
            api = Mock()
            api.create_commit.return_value = SimpleNamespace(commit_url="https://example.test/commit")
            with patch.object(cloud_state, "PROJECT_ROOT", root), \
                 patch.object(cloud_state, "DATA", root / "data"), \
                 patch("huggingface_hub.HfApi", return_value=api):
                result = cloud_state.save("user/dataset", "state/checkpoints/test", token="test")
            api.create_commit.assert_called_once()
            operations = api.create_commit.call_args.kwargs["operations"]
            self.assertEqual(len(operations), 2)
            manifest = json.loads(operations[1].path_or_fileobj)
            self.assertEqual(len(manifest["archive_sha256"]), 64)
            self.assertEqual(result["files"], 1)

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


if __name__ == "__main__":
    unittest.main()
