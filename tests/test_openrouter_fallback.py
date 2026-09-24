from __future__ import annotations

import unittest

from sansad_pipeline.openrouter import (
    OpenRouterAdjudicator,
    OpenRouterHTTPError,
    OpenRouterRateLimitError,
)


class FakeAdjudicator(OpenRouterAdjudicator):
    def __init__(self):
        self.attempts = []

    def configured_models(self, override=None):
        return [override] if override else ["primary", "fallback"]

    def _page_numbers(self, identifier, pages, *, all_pages=False):
        return 1, pages or [7]

    def adjudicate(self, identifier, *, pages=None, model=None, all_pages=False):
        self.attempts.append((pages[0], model))
        if model == "primary":
            raise OpenRouterHTTPError(400, "provider moderation failure")
        return pages


class RateLimitedAdjudicator(FakeAdjudicator):
    def adjudicate(self, identifier, *, pages=None, model=None, all_pages=False):
        self.attempts.append((pages[0], model))
        raise OpenRouterHTTPError(429, "provider capacity exhausted")


class OpenRouterFallbackTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

