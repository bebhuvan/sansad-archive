from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from sansad_pipeline import cli
from sansad_pipeline.census import Census
from sansad_pipeline.config import Config, StorageConfig
from sansad_pipeline.sources.questions import QuestionRecord
from scripts import extract_cloud_scope


class ExtractCloudScopeTests(unittest.TestCase):
    def test_pending_selection_uses_current_extraction_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "pipeline.toml"
            config_path.write_text('[storage]\nroot = "data"\n', encoding="utf-8")
            config = Config(project_root=root, storage=StorageConfig(root=Path("data")))
            source = root / "source.pdf"
            Image.new("RGB", (80, 80), "white").save(source, "PDF")
            census = Census(config)
            document = census.store.ingest(source)
            record = QuestionRecord(
                record_id="elibrary_test", source_type="questions_answers",
                house="lok_sabha", parliament_number="18", session="8",
                document_number="1", document_subtype="UNSTARRED",
                document_date="2026-01-01", title="Test", ministry="TEST",
                members=[], language="en", source_url="https://example.test/test",
                official_page_url="https://example.test", api_url="https://example.test/api",
                api_params={}, raw={},
            )
            census._upsert_many([record])
            census.store.db.execute(
                "UPDATE census_records SET document_sha256=?,acquisition_status='downloaded'",
                (document.sha256,),
            )
            args = [
                "--config", str(config_path), "process-scope", "--house", "lok_sabha",
                "--parliament", "18", "--session", "8", "--pending-only",
                "--limit", "1", "--workers", "2", "--summary-out", str(root / "summary.json"),
            ]
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(json.loads((root / "summary.json").read_text())["selected"], 1)
            self.assertEqual(cli.main(args), 0)
            self.assertEqual(json.loads((root / "summary.json").read_text())["selected"], 0)

    def test_checkpoints_each_chunk_and_stops_on_failure(self):
        args = SimpleNamespace(
            house="lok_sabha", parliament="18", session="8", repo="test/dataset",
            checkpoint_path="state/checkpoints/test", chunk_documents=100,
            workers=2, reserve_seconds=0,
        )
        summaries = [
            {"selected": 100, "completed": 100, "failures": 0},
            {"selected": 2, "completed": 1, "failures": 1},
        ]
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "process-scope" in command:
                index = sum("process-scope" in call for call in calls) - 1
                path = Path(command[command.index("--summary-out") + 1])
                path.write_text(json.dumps(summaries[index]), encoding="utf-8")
                return SimpleNamespace(returncode=0 if index == 0 else 1)
            return SimpleNamespace(returncode=0)

        with patch.dict("os.environ", {"JOB_DEADLINE": "0"}), \
             patch.object(extract_cloud_scope.subprocess, "run", side_effect=run):
            self.assertEqual(extract_cloud_scope.extract(args), 1)
        self.assertEqual(sum("process-scope" in call for call in calls), 2)
        self.assertEqual(sum("scripts/cloud_state.py" in call for call in calls), 2)


if __name__ == "__main__":
    unittest.main()
