import unittest

from scripts.acquire_cloud_scope import build_parser


class AcquireCloudScopeTests(unittest.TestCase):
    def test_default_chunk_bounds_uncheckpointed_acquisition(self):
        required = [
            "--house", "lok_sabha", "--parliament", "01", "--session", "III",
            "--source", "elibrary", "--repo", "user/dataset",
            "--checkpoint-path", "state/checkpoints/example",
        ]
        self.assertEqual(build_parser().parse_args(required).chunk, 250)
        self.assertEqual(build_parser().parse_args([*required, "--chunk", "1000"]).chunk,
                         1000)


if __name__ == "__main__":
    unittest.main()
