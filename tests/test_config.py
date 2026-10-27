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
    def test_repository_defaults_disable_second_stage(self):
        config = kb_repo_url.load_config(ROOT / "config.yaml")
        self.assertFalse(config["two_stage_enabled"])
        self.assertEqual(config["second_stage_mode"], "tokens")
        self.assertEqual(config["second_stage_budget_tokens"], 150000)
        self.assertEqual(config["workers"], 12)
        self.assertEqual(config["map_tokens"], 10000)
        self.assertEqual(config["model"], "gpt-6.1-sol")
        self.assertEqual(config["effort"], "xhigh")

    def test_omitted_two_stage_config_is_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.yaml"
            path.write_text("")
            self.assertFalse(kb_repo_url.load_config(path)["two_stage_enabled"])

    def test_count_config_without_pyyaml(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "count.yaml"
            path.write_text("two_stage:\n  enabled: true\n  mode: count\n  count: 7\n")
            with mock.patch.object(kb_repo_url, "yaml", None):
                config = kb_repo_url.load_config(path)
        self.assertTrue(config["two_stage_enabled"])
        self.assertEqual(config["second_stage_mode"], "count")
        self.assertEqual(config["second_stage_count"], 7)

    def test_cli_enables_only_requested_grouping_and_still_mints(self):
        cases = [
            ([], None),
            # config model gpt-6.1-sol has a doubled context window, so 150000 becomes 300000.
            (["--two-stage"], ["--budget-tokens", "300000"]),
            (["--second-stage-count", "3"], ["--blob-count", "3"]),
            (["--second-stage-budget-tokens", "90000"], ["--budget-tokens", "90000"]),
            (["--no-two-stage", "--second-stage-count", "3"], None),
        ]
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            repo_map = source / "map.txt"
            repo_map.write_text("app.py\n")
            for options, expected in cases:
                argv = ["kb_repo_url.py", str(source), "--name", "example", "--repo-map", str(repo_map), *options]
                with self.subTest(options=options), \
                        mock.patch.object(kb_repo_url.sys, "argv", argv), \
                        mock.patch.object(kb_repo_url, "clone_repository"), \
                        mock.patch.object(kb_repo_url.subprocess, "check_output", return_value="HEAD\n"), \
                        mock.patch.object(kb_repo_url.kb_api, "rr_configuration"), \
                        mock.patch.object(kb_repo_url, "run") as run:
                    kb_repo_url.main()
                commands = [call.args[0] for call in run.call_args_list]
                merges = [command for command in commands if "merge-old" in command]
                self.assertTrue(commands[-1][1].endswith("kb_fork_mint.py"))
                if expected is None:
                    self.assertEqual(merges, [])
                else:
                    self.assertEqual(len(merges), 1)
                    self.assertEqual(merges[0][-2:], expected)

    def test_double_context_model_doubles_config_budgets_but_not_cli_values(self):
        cases = [
            ([], ["300000", "20000", "300000"]),
            (["--model", "gpt-6-sol"], ["150000", "10000", "150000"]),
            (["--budget-tokens", "120000", "--map-tokens", "8000"], ["120000", "8000", "300000"]),
        ]
        def write_map(repo, output, tokens):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("app.py\n")

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            for options, (budget, map_tokens, merge_budget) in cases:
                argv = ["kb_repo_url.py", str(source), "--name", "example", "--two-stage", *options]
                with self.subTest(options=options), \
                        mock.patch.object(kb_repo_url.sys, "argv", argv), \
                        mock.patch.object(kb_repo_url, "clone_repository"), \
                        mock.patch.object(kb_repo_url.subprocess, "check_output", return_value="HEAD\n"), \
                        mock.patch.object(kb_repo_url.kb_api, "rr_configuration"), \
                        mock.patch.object(kb_repo_url.kb_api, "STATE_ROOT", temp), \
                        mock.patch.object(kb_repo_url, "generate_repo_map",
                                          side_effect=write_map) as generate, \
                        mock.patch.object(kb_repo_url, "run") as run:
                    kb_repo_url.main()
                commands = [call.args[0] for call in run.call_args_list]
                build = commands[0]
                self.assertEqual(build[build.index("--budget-tokens") + 1], budget)
                self.assertEqual(str(generate.call_args.args[2]), map_tokens)
                merge = [command for command in commands if "merge-old" in command][0]
                self.assertEqual(merge[-2:], ["--budget-tokens", merge_budget])

    def test_context_scale(self):
        self.assertEqual(kb_repo_url.kb_api.context_scale("gpt-6.1-sol"), 2)
        self.assertEqual(kb_repo_url.kb_api.context_scale("gpt-6-sol"), 1)

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
