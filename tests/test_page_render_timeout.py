from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sansad_pipeline.openrouter import RENDER_TIMEOUT_SECONDS, render_page


class PageRenderTimeoutTests(unittest.TestCase):
    def test_screenshot_timeout_identifies_page_and_has_finite_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "source.pdf"
            with mock.patch("sansad_pipeline.openrouter.subprocess.run",
                            side_effect=subprocess.TimeoutExpired("lit", RENDER_TIMEOUT_SECONDS)) as run:
                with self.assertRaisesRegex(RuntimeError, "timed out.*page 3"):
                    render_page(pdf, 3, Path(directory) / "image", 250)
            self.assertEqual(run.call_args.kwargs["timeout"], RENDER_TIMEOUT_SECONDS)

    def test_screenshot_failure_exposes_bounded_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "source.pdf"
            failure = subprocess.CalledProcessError(1, ["lit"], stderr="bad source PDF")
            with mock.patch("sansad_pipeline.openrouter.subprocess.run", side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, "page 2.*bad source PDF"):
                    render_page(pdf, 2, Path(directory) / "image", 250)


if __name__ == "__main__":
    unittest.main()
