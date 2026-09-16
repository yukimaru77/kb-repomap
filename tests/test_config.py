import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_loader(
    "kb_repo_url", SourceFileLoader("kb_repo_url", str(ROOT / "kb_repo_url.py"))
)
kb_repo_url = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kb_repo_url)


class ConfigTest(unittest.TestCase):
    def test_repository_defaults_enable_token_budgeted_second_stage(self):
        config = kb_repo_url.load_config(ROOT / "config.yaml")
        self.assertTrue(config["two_stage_enabled"])
        self.assertEqual(config["second_stage_budget_tokens"], 150000)
        self.assertEqual(config["workers"], 10)
        self.assertEqual(config["map_tokens"], 10000)

    def test_reads_edited_yaml_without_pyyaml(self):
        content = """
chunk_tokens: 120000
two_stage:
  enabled: true
  token_budget: 140000
model: gpt-test
effort: medium
workers: 3
max_file_bytes: 500000
thin_blob_threshold: 1000
"""
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            config_path.write_text(content)
            with mock.patch.object(kb_repo_url, "yaml", None):
                config = kb_repo_url.load_config(config_path)
        self.assertEqual(config["chunk_tokens"], 120000)
        self.assertTrue(config["two_stage_enabled"])
        self.assertEqual(config["second_stage_budget_tokens"], 140000)
        self.assertEqual(config["model"], "gpt-test")

    def test_managed_clone_never_smudges_git_lfs_objects(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(kb_repo_url, "run") as run:
            destination = Path(temp) / "clones" / "example"
            kb_repo_url.clone_repository("https://example.invalid/repo.git", destination, "main")

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"]["GIT_LFS_SKIP_SMUDGE"], "1")

    def test_map_generation_calls_extracted_engine_without_codex(self):
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch("repo_map.generate_map", return_value="src/app.py:\n") as generate:
                with mock.patch.object(kb_repo_url, "run") as run:
                    output = Path(temp) / "map.txt"
                    kb_repo_url.generate_repo_map(Path(temp), output, 10000)
                    self.assertEqual(output.read_text(), "src/app.py:\n")
                    generate.assert_called_once_with(Path(temp), map_tokens=10000)
                    run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
