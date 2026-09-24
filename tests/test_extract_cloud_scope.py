from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sansad_pipeline import cli
from sansad_pipeline.config import Config
from scripts import extract_cloud_scope


class ExtractCloudScopeTests(unittest.TestCase):
    def test_pending_selection_uses_current_extraction_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.json"
            store = Mock()
            store.db.all.return_value = [{"document_sha256": "a" * 64}]
            pipeline = Mock()
            pipeline.process.return_value = 9
            with patch.object(cli, "load_config", return_value=Config(project_root=Path(directory))), \
                 patch.object(cli, "Store", return_value=store), \
                 patch.object(cli, "Pipeline", return_value=pipeline):
                result = cli.main([
                    "process-scope", "--house", "lok_sabha", "--parliament", "18",
                    "--session", "8", "--pending-only", "--limit", "1",
                    "--summary-out", str(summary),
                ])
            self.assertEqual(result, 0)
            query, params = store.db.all.call_args.args
            self.assertIn("NOT EXISTS", query)
            self.assertIn("r.config_json=?", query)
            self.assertEqual(params[-1], 1)
            self.assertIn('"liteparse"', params[-2])
            self.assertEqual(json.loads(summary.read_text()), {
                "selected": 1, "completed": 1, "failures": 0,
            })

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
