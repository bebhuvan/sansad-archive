from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sansad_pipeline.census import Census
from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.sources.questions import QuestionRecord


class AcquisitionRetryTests(unittest.TestCase):
    def test_retry_pass_visits_more_than_one_batch_of_failed_records(self):
        with tempfile.TemporaryDirectory() as directory:
            census = Census(Config(project_root=Path(directory),
                                   storage=StorageConfig(root=Path("data"))))
            census._upsert_many([
                QuestionRecord(
                    record_id=f"q{i:03d}", source_type="questions_answers",
                    house="lok_sabha", parliament_number="18", session="8",
                    document_number=str(i), document_subtype="S", document_date="",
                    title="", ministry="", members=[], language="en",
                    source_url=f"https://example.test/{i}.pdf", official_page_url="",
                    api_url="", api_params={}, raw={},
                ) for i in range(45)
            ])
            census.store.db.execute("UPDATE census_records SET acquisition_status='failed'")
            with patch.object(census.store, "download", side_effect=ValueError("not a PDF")) as download:
                with redirect_stdout(io.StringIO()):
                    result = census.acquire_questions(
                        source="current", house="lok_sabha", lok_sabha="18",
                        session="8", retry_failed=True, workers=4, min_free_gib=0,
                    )
            self.assertEqual(result["selected"], 45)
            self.assertEqual(download.call_count, 45)


if __name__ == "__main__":
    unittest.main()
