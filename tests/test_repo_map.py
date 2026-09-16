from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_map import generate_map, repository_files


class RepoMapTest(unittest.TestCase):
    def test_extracts_symbols_without_llm_and_respects_aiderignore(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            files = {
                "app.py": "from library import greet\ndef run():\n    return greet('KB')\n",
                "library.py": "def greet(name):\n    return 'hello ' + name\n",
                "app.js": "export function render(name) { return name; }\n",
                "ignored.py": "def excluded_secret(): pass\n",
                ".aiderignore": "ignored.py\n",
            }
            for name, body in files.items():
                (repo / name).write_text(body)
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            self.assertNotIn(str(repo / "ignored.py"), repository_files(repo))
            # Warm the tokenizer data before denying any network connections.
            from repo_map import TokenCounter
            TokenCounter()
            with mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
                first = generate_map(repo, cache_dir=Path(temp) / "cache")
                cached = generate_map(repo, cache_dir=Path(temp) / "cache")
            self.assertEqual(first, cached)
            self.assertIn("def greet(name):", first)
            self.assertIn("def run():", first)
            self.assertIn("function render", first)
            self.assertNotIn("excluded_secret", first)
            self.assertFalse(list(repo.glob(".aider.tags*")))


if __name__ == "__main__":
    unittest.main()
