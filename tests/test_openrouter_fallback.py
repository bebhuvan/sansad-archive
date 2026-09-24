from __future__ import annotations

import unittest
import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sansad_pipeline.openrouter import (
    OpenRouterAdjudicator,
    OpenRouterCostViolationError,
    OpenRouterHTTPError,
    OpenRouterRateLimitError,
    checked_reported_cost,
)


class FakeAdjudicator(OpenRouterAdjudicator):
    def __init__(self):
        self.attempts = []

    def configured_models(self, override=None):
        return [override] if override else ["primary", "fallback"]

    def _page_numbers(self, identifier, pages, *, all_pages=False, include_ocr=False):
        return 1, pages or [7]

    def adjudicate(
        self, identifier, *, pages=None, model=None, all_pages=False, include_ocr=False
    ):
        self.attempts.append((pages[0], model))
        if model == "primary":
            raise OpenRouterHTTPError(400, "provider moderation failure")
        return pages


class RateLimitedAdjudicator(FakeAdjudicator):
    def adjudicate(
        self, identifier, *, pages=None, model=None, all_pages=False, include_ocr=False
    ):
        self.attempts.append((pages[0], model))
        raise OpenRouterHTTPError(429, "provider capacity exhausted")


class CostViolationAdjudicator(FakeAdjudicator):
    def adjudicate(
        self, identifier, *, pages=None, model=None, all_pages=False, include_ocr=False
    ):
        self.attempts.append((pages[0], model))
        raise OpenRouterCostViolationError("provider reported a charge")


class OpenRouterFallbackTests(unittest.TestCase):
    def test_nonzero_or_invalid_reported_cost_stops_free_only_calls(self):
        self.assertIsNone(checked_reported_cost({}))
        self.assertEqual(str(checked_reported_cost({"cost": 0})), "0")
        for value in (0.0001, "unknown", "NaN"):
            with self.subTest(value=value), self.assertRaises(OpenRouterCostViolationError):
                checked_reported_cost({"cost": value})

    def test_persisted_cost_stop_refuses_resumed_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "artifacts" / ".openrouter-paid-stop.json"
            marker.parent.mkdir()
            marker.write_text("{}")
            worker = FakeAdjudicator()
            worker.config = SimpleNamespace(data_root=root, openrouter=SimpleNamespace())
            with self.assertRaisesRegex(OpenRouterCostViolationError, "prior OpenRouter cost violation"):
                OpenRouterAdjudicator.adjudicate(worker, "document")

    def test_paid_model_is_rejected_before_any_page_call(self):
        worker = FakeAdjudicator()
        worker.config = SimpleNamespace(openrouter=SimpleNamespace(
            models_endpoint="https://example.test/models", timeout_seconds=5
        ))
        worker._snapshot_cache = {}
        payload = {"data": [{"id": "primary", "architecture": {
            "input_modalities": ["text", "image"]},
            "pricing": {"prompt": "0.0001", "completion": "0"}}]}
        response = io.BytesIO(json.dumps(payload).encode())
        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "paid calls are disabled"):
                worker._model_snapshot("primary")

    def test_retryable_failure_uses_second_model(self):
        worker = FakeAdjudicator()
        result = worker.adjudicate_with_fallback("document", pages=[7], force=True)
        self.assertEqual(worker.attempts, [(7, "primary"), (7, "fallback")])
        self.assertEqual(result["completed"], [{"page": 7, "model": "fallback"}])
        self.assertFalse(result["failed"])

    def test_every_model_rate_limited_stops_the_command(self):
        worker = RateLimitedAdjudicator()
        with self.assertRaises(OpenRouterRateLimitError):
            worker.adjudicate_with_fallback("document", pages=[7], force=True)
        self.assertEqual(worker.attempts, [(7, "primary"), (7, "fallback")])

    def test_cost_violation_does_not_try_fallback_model(self):
        worker = CostViolationAdjudicator()
        with self.assertRaises(OpenRouterCostViolationError):
            worker.adjudicate_with_fallback("document", pages=[7], force=True)
        self.assertEqual(worker.attempts, [(7, "primary")])


if __name__ == "__main__":
    unittest.main()
