import io
import subprocess
import unittest
from unittest import mock

import kb_native_local as native


ROOT_HELP = """Usage: codex [OPTIONS] [PROMPT]
Commands:
  exec    Run Codex [aliases: e]
  resume  Resume a session
  fork    Fork a session
  review  Review code
  login   Log in
Options:
  -c, --config <key=value>
  -C, --cd <DIR>
  -m, --model <MODEL>
      --no-alt-screen
  -h, --help
"""
EXEC_HELP = """Usage: codex exec [OPTIONS] [PROMPT]
Commands:
  resume  Resume a session
  fork    Fork a session
  review  Review code
Options:
  -c, --config <key=value>
  -C, --cd <DIR>
  -m, --model <MODEL>
  -o, --output-last-message <FILE>
  -i, --image <FILE>...
      --output-schema <FILE>
      --future-widget <VALUE>
      --json
      --ephemeral
      --skip-git-repo-check
  -h, --help
"""


class NativeLocalTests(unittest.TestCase):
    def setUp(self):
        native._metadata.cache_clear()

        def help_output(argv, **kwargs):
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(argv[-1], "--help")
            help_text = EXEC_HELP if "exec" in argv else ROOT_HELP
            if "resume" in argv:
                help_text = help_text.replace("<FILE>...", "<FILE>")
            return subprocess.CompletedProcess(argv, 0, help_text, "")

        patcher = mock.patch.object(native.subprocess, "run", side_effect=help_output)
        self.help = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(native._metadata.cache_clear)

    def test_exec_keeps_all_native_settings_on_parent_parser(self):
        flags = ["-C", "/work space", "--json", "-m", "gpt-6-astra", "-c", "features.foo=true",
                 "-o", "/tmp/last message", "--output-schema", "/tmp/schema.json", "--ephemeral"]
        self.assertEqual(native.command(["exec", *flags, "Explain the code"], "seed"),
                         ["codex", "exec", *flags, "resume", "seed", "Explain the code"])

    def test_native_stdin_is_not_consumed_or_replaced(self):
        stdin = io.StringIO("user prompt from pipe")
        with mock.patch("sys.stdin", stdin):
            self.assertEqual(native.command(["exec", "--json", "-"], "seed"),
                             ["codex", "exec", "--json", "resume", "seed", "-"])
            self.assertEqual(native.command(["exec", "--json"], "seed"),
                             ["codex", "exec", "--json", "resume", "seed"])
        self.assertEqual(stdin.tell(), 0)

    def test_discovers_new_options_and_preserves_option_values_named_like_commands(self):
        self.assertEqual(native.command(["exec", "--future-widget", "review", "answer", "--json"], "seed"),
                         ["codex", "exec", "--future-widget", "review", "--json", "resume", "seed", "answer"])
        self.assertEqual(native.command(["-C", "exec", "e", "--future-widget=resume", "-mnew", "hi"], "seed"),
                         ["codex", "-C", "exec", "e", "--future-widget=resume", "-mnew", "resume", "seed", "hi"])

    def test_tui_flags_and_end_of_options_are_preserved(self):
        args = ["-C", "/work", "-m", "model", "--no-alt-screen", "hello"]
        self.assertEqual(native.command(args, "seed"), ["codex", "resume", "seed", *args])
        self.assertEqual(native.command(["--", "exec"], "seed"),
                         ["codex", "resume", "seed", "--", "exec"])
        self.assertEqual(native.command(["exec", "--json", "--", "--a literal prompt"], "seed"),
                         ["codex", "exec", "--json", "resume", "seed", "--", "--a literal prompt"])

    def test_explicit_existing_session_or_review_is_never_silently_replaced(self):
        for args in (["resume", "old"], ["fork", "old"], ["review"], ["exec", "resume", "old"],
                     ["exec", "fork", "old"], ["exec", "review"], ["-m", "model", "resume", "old"]):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, "--remote"):
                native.command(args, "seed")

    def test_unknown_options_reach_native_validation_and_missing_values_fail_early(self):
        self.assertEqual(native.command(["exec", "--future-unknown", "hello"], "seed"),
                         ["codex", "exec", "--future-unknown", "resume", "seed", "hello"])
        with self.assertRaisesRegex(ValueError, "--model"):
            native.command(["exec", "--model"], "seed")

    def test_help_metadata_is_reused_across_validation_and_launch(self):
        native.command(["exec", "hello"], "validation-only")
        native.command(["exec", "hello"], "seed")
        self.assertEqual(self.help.call_count, 2)

    def test_subcommand_index_distinguishes_flag_values_and_literal_prompts(self):
        self.assertEqual(native.subcommand_index(["-m", "resume", "-C", "exec", "e", "hello"]), 4)
        self.assertEqual(native.subcommand_index(["--model=resume", "exec", "hello"]), 1)
        self.assertIsNone(native.subcommand_index(["-m", "exec", "hello"]))
        self.assertIsNone(native.subcommand_index(["--", "exec"]))

    def test_seed_overrides_preserves_config_forms_and_model_order(self):
        self.assertEqual(native.seed_overrides([
            "-c", "base_instructions='local'", "exec", "-m", "gpt-6-astra",
            "--config=model_reasoning_effort='low'", "-cfeatures.foo=true",
            "--model=next-model", "-mfinal-model", "hello",
        ]), ["base_instructions='local'", "model_reasoning_effort='low'", "features.foo=true",
             'model="final-model"'])
        self.assertEqual(native.seed_overrides(["exec", "-m", "explicit", "-c", 'model="config"', "hi"]),
                         ['model="config"', 'model="explicit"'])

    def test_seed_overrides_does_not_inspect_prompt_stdin_or_other_option_values(self):
        stdin = io.StringIO("-mnot-a-setting")
        with mock.patch("sys.stdin", stdin):
            self.assertEqual(native.seed_overrides([
                "exec", "--future-widget", "-mnot-a-setting", "-i", "a.png,b.png",
                "--config", 'base_instructions="quoted \\\"text\\\""', "-", "--", "--model=literal",
            ]), ['base_instructions="quoted \\\"text\\\""'])
        self.assertEqual(stdin.tell(), 0)
        self.assertEqual(native.seed_overrides(["--", "-mmust-remain-a-prompt"]), [])

    def test_variadic_images_keep_native_arity_without_swallowing_inserted_resume(self):
        self.assertEqual(native.command(["exec", "-i", "a.png", "b.png", "--json", "prompt"], "seed"),
                         ["codex", "exec", "--json", "resume", "seed", "-i", "a.png", "-i", "b.png", "prompt"])
        self.assertEqual(native.command(["exec", "--image=a.png,b.png", "c.png", "--", "prompt"], "seed"),
                         ["codex", "exec", "resume", "seed", "--image=a.png,b.png", "--image", "c.png", "--", "prompt"])
        self.assertEqual(native.seed_overrides(["exec", "--image", "a.png", "b.png", "-m", "model", "prompt"]),
                         ['model="model"'])
