from __future__ import annotations

import unittest

from sansad_pipeline.sources.elibrary import records_from_search_response, search_page


class ElibrarySourceTests(unittest.TestCase):
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
        self.assertEqual(record.raw["uuid"], "abc-123")
        self.assertNotIn("metadata", record.raw)
        self.assertEqual(record.api_params["page"], 7)

    def test_rejects_page_size_above_server_cap_before_network(self):
        with self.assertRaises(ValueError):
            search_page(page_size=101)


if __name__ == "__main__":
    unittest.main()
