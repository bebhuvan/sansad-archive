import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from sansad_pipeline.config import Config, NvidiaConfig, StorageConfig
from sansad_pipeline.nvidia import NvidiaAdjudicator, NvidiaHTTPError


class _Response:
    status = 200
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b'{"choices":[{"message":{"content":"ok"}}]}'


class NvidiaTests(unittest.TestCase):
    def test_retries_429_and_records_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = Config(
                project_root=root,
                storage=StorageConfig(root=root / "data"),
                nvidia=NvidiaConfig(
                    enabled=True,
                    minimum_interval_seconds=0,
                    max_retries=1,
                    maximum_backoff_seconds=1,
                ),
            )
            waits = []
            worker = NvidiaAdjudicator(config, sleep=waits.append, random_value=lambda: 0)
            error = urllib.error.HTTPError(
                "https://example.invalid", 429, "busy", {"Retry-After": "0.5"}, io.BytesIO(b"busy")
            )
            log = root / "attempts.json"
            with patch("urllib.request.urlopen", side_effect=[error, _Response()]):
                payload, _ = worker._post(object(), log)
            self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
            self.assertEqual(waits, [0.5])
            self.assertIn('"http_status": 429', log.read_text())
            self.assertIn('"http_status": 200', log.read_text())

    def test_non_retryable_error_is_immediate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = Config(project_root=root, storage=StorageConfig(root=root / "data"))
            worker = NvidiaAdjudicator(config, sleep=lambda _: None)
            error = urllib.error.HTTPError(
                "https://example.invalid", 401, "no", {}, io.BytesIO(b"no")
            )
            with patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(NvidiaHTTPError):
                    worker._post(object(), root / "attempts.json")


if __name__ == "__main__":
    unittest.main()
