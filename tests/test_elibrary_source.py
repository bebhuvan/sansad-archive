from __future__ import annotations

import unittest
from unittest.mock import patch

from sansad_pipeline.sources.elibrary import (
    classify_language, list_original_pdfs, records_from_search_response, search_page,
)


class ElibrarySourceTests(unittest.TestCase):
    def test_lists_every_original_pdf_without_silently_truncating(self):
        pdfs = [
            {"uuid": name, "name": f"{name}.pdf", "_links": {"content": {"href": f"https://example.test/{name}"}}}
            for name in ("english", "hindi")
        ]
        bundles = {"_embedded": {"bundles": [
            {"name": "ORIGINAL", "_links": {"bitstreams": {"href": "https://example.test/bitstreams"}}}
        ]}}
        listing = {"page": {"number": 0, "totalElements": 2},
                   "_embedded": {"bitstreams": pdfs}}
        with patch("sansad_pipeline.sources.elibrary.request_json", side_effect=[bundles, listing]):
            resolved = list_original_pdfs("item")
        self.assertEqual([pdf["uuid"] for _, pdf in resolved], ["english", "hindi"])

        listing["page"]["totalElements"] = 3
        with patch("sansad_pipeline.sources.elibrary.request_json", side_effect=[bundles, listing]):
            with self.assertRaisesRegex(RuntimeError, "incomplete"):
                list_original_pdfs("item")

    def test_includes_pdf_bitstream_even_without_pdf_filename_suffix(self):
        bundles = {"_embedded": {"bundles": [
            {"name": "ORIGINAL", "_links": {"bitstreams": {"href": "https://example.test/bitstreams"}}}
        ]}}
        listing = {"page": {"number": 0, "totalElements": 1},
                   "_embedded": {"bitstreams": [{
                       "uuid": "bitstream", "name": "question scan",
                       "_links": {
                           "content": {"href": "https://example.test/content"},
                           "format": {"href": "https://example.test/format"},
                       },
                   }]}}
        with patch("sansad_pipeline.sources.elibrary.request_json",
                   side_effect=[bundles, listing, {"mimetype": "application/pdf"}]):
            resolved = list_original_pdfs("item")
        self.assertEqual([pdf["uuid"] for _, pdf in resolved], ["bitstream"])

    def test_unlabelled_or_original_language_is_not_assumed_english(self):
        self.assertEqual(classify_language(["English"]), "en")
        self.assertEqual(classify_language(["Hindi"]), "hi")
        self.assertEqual(classify_language(["Original"]), "und")
        self.assertEqual(classify_language([]), "und")

    def test_normalizes_item_without_storing_full_hal_payload(self):
        item = {
            "uuid": "abc-123",
            "handle": "123456789/42",
            "name": "A question",
            "metadata": {
                "dc.title": [{"value": "A question"}],
                "dc.date.issued": [{"value": "1959-11-25"}],
                "dc.identifier.loksabhanumber": [{"value": "02"}],
                "dc.identifier.sessionnumber": [{"value": "IX"}],
                "dc.identifier.questionnumber": [{"value": "485"}],
                "dc.identifier.questiontype": [{"value": "Unstarred"}],
                "dc.contributor.members": [{"value": "A Member"}],
                "dc.language.iso": [{"value": "Original"}],
            },
        }
        response = {
            "_embedded": {
                "searchResult": {
                    "_embedded": {"objects": [{"_embedded": {"indexableObject": item}}]}
                }
            }
        }
        record = records_from_search_response(response, page=7)[0]
        self.assertEqual(record.parliament_number, "02")
        self.assertEqual(record.document_number, "485")
        self.assertEqual(record.language, "und")
        self.assertEqual(record.raw["uuid"], "abc-123")
        self.assertNotIn("metadata", record.raw)
        self.assertEqual(record.api_params["page"], 7)

        item["metadata"]["dc.identifier.sessionnumber"] = [{"value": "A MemberIX"}]
        repaired = records_from_search_response(response, page=7)[0]
        self.assertEqual(repaired.session, "IX")
        self.assertEqual(repaired.raw["session_normalization"]["original"], "A MemberIX")

    def test_rejects_page_size_above_server_cap_before_network(self):
        with self.assertRaises(ValueError):
            search_page(page_size=101)

    def test_rejects_truncated_search_page(self):
        response = {"_embedded": {"searchResult": {
            "page": {"number": 4, "size": 2, "totalPages": 5, "totalElements": 10},
            "_embedded": {"objects": [{}]},
        }}}
        with patch("sansad_pipeline.sources.elibrary.request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "incomplete or inconsistent"):
                search_page(page=4, page_size=2)

    def test_rejects_misnumbered_search_page(self):
        response = {"_embedded": {"searchResult": {
            "page": {"number": 3, "size": 2, "totalPages": 5, "totalElements": 10},
            "_embedded": {"objects": [{}, {}]},
        }}}
        with patch("sansad_pipeline.sources.elibrary.request_json", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "incomplete or inconsistent"):
                search_page(page=4, page_size=2)

    def test_rejects_unidentified_and_duplicate_items(self):
        def response(items):
            return {"_embedded": {"searchResult": {
                "_embedded": {"objects": [
                    {"_embedded": {"indexableObject": item}} for item in items
                ]},
            }}}
        with self.assertRaisesRegex(RuntimeError, "without an ID"):
            records_from_search_response(response([{}]), page=7)
        with self.assertRaisesRegex(RuntimeError, "repeats item"):
            records_from_search_response(
                response([{"uuid": "same"}, {"uuid": "same"}]), page=7
            )


if __name__ == "__main__":
    unittest.main()
