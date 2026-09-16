import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SourceStatsTest(unittest.TestCase):
    def test_measures_filtered_source_without_codex_or_repo_map(self):
        with tempfile.TemporaryDirectory() as temp:
            repository = Path(temp) / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "app.py").write_text("print('hello')\n")
            (repository / "image.png").write_bytes(b"not-real-image")
            subprocess.run(
                ["git", "-C", str(repository), "add", "app.py", "image.png"],
                check=True,
            )
            subprocess.run([
                "git", "-C", str(repository), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "initial",
            ], check=True)

            result = subprocess.run([
                sys.executable, str(ROOT / "kb_source_stats.py"),
                "--repo", str(repository),
            ], check=True, capture_output=True, text=True)
            stats = json.loads(result.stdout)

        self.assertEqual(stats["included_files"], 1)
        self.assertEqual(stats["skipped_files"], 1)
        self.assertGreater(stats["source_total_tokens"], 0)
        self.assertEqual(
            stats["skip_reasons"]["binary/asset/certificate suffix"], 1
        )


if __name__ == "__main__":
    unittest.main()
