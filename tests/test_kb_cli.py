import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_cli
import kb_codex
import kb_store


class GitStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(kb_store, "CACHE", self.root / "cache").start()
        mock.patch.object(kb_store, "CONFIG", self.root / "config.json").start()
        mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        }).start()
        self.source = self.repo("source")
        (self.source / "code.py").write_text("old value\n")
        self.base = self.commit(self.source)
        (self.source / "code.py").write_text("new value\n")
        self.head = self.commit(self.source)
        self.info = {"repository_url": str(self.source), "source_commit": self.base, "branch": "main"}
        self.store1 = {"name": "first", "url": str(self.repo("store1"))}
        self.store2 = {"name": "second", "url": str(self.repo("store2"))}
        # Publish to bare origins so the integration test uses ordinary Git push.
        for entry in (self.store1, self.store2):
            bare = Path(entry["url"] + ".git")
            kb_store.run(["git", "clone", "--bare", entry["url"], str(bare)])
            entry["url"] = str(bare)
        self.config = {"stores": [self.store1, self.store2]}
        self.jsonl = self.root / "input.jsonl"
        self.blobs = [{"type": "compaction", "id": str(i), "encrypted_content": f"opaque-{i}",
                       "unknown": {"keep": True}} for i in range(35)]
        values = [{"type": "session_meta", "payload": {"id": "019f834a-9d8a-7eae-bf4f-1a54d8d74a92"}}]
        values += [{"type": "response_item", "payload": blob} for blob in self.blobs]
        self.jsonl.write_text("".join(json.dumps(v) + "\n" for v in values))

    def repo(self, name):
        path = self.root / name
        kb_store.run(["git", "init", "-q", "-b", "main", str(path)])
        (path / "README.md").write_text(name)
        self.commit(path)
        return path

    def commit(self, path):
        kb_store.git(path, "add", ".")
        kb_store.git(path, "commit", "-qm", "fixture")
        return kb_store.git(path, "rev-parse", "HEAD").stdout.strip()

    def test_multiple_stores_priority_and_explicit_selection(self):
        kb_store.publish(self.store2, "example", self.info, self.jsonl)
        loaded = kb_store.find_kb(self.config, "example")
        self.assertEqual(loaded["store"]["name"], "second")
        kb_store.publish(self.store1, "example", self.info, self.jsonl)
        self.assertEqual(kb_store.find_kb(self.config, "example")["store"]["name"], "first")
        self.assertEqual(kb_store.find_kb(self.config, "example", "second")["store"]["name"], "second")
        self.assertEqual(loaded["jsonl"].read_bytes(), self.jsonl.read_bytes())

    def test_git_history_updates_pair_and_fetches_newest_snapshot(self):
        first = kb_store.publish(self.store1, "example", self.info, self.jsonl)
        before = kb_store.find_kb(self.config, "example")["jsonl"].read_bytes()
        raw = self.jsonl.read_bytes() + b'\n'
        self.jsonl.write_bytes(raw)
        latest_info = {**self.info, "source_commit": self.head}
        second = kb_store.publish(self.store1, "example", latest_info, self.jsonl)
        loaded = kb_store.find_kb(self.config, "example")
        self.assertNotEqual(first, second)
        self.assertEqual(loaded["jsonl"].read_bytes(), raw)
        self.assertEqual(loaded["info"], latest_info)
        self.assertEqual(kb_store.git(self.store1["url"], "show", f"{first}:example/latest.jsonl", text=False).stdout, before)
        self.assertEqual(kb_store.git(self.store1["url"], "ls-tree", "--name-only", f"{second}:example").stdout.splitlines(),
                         ["info.json", "latest.jsonl"])

    def test_git_comparison_includes_diff_and_detects_same_commit(self):
        head, context = kb_store.source_update(self.info)
        self.assertEqual(head, self.head)
        self.assertIn("-old value", context)
        self.assertIn("+new value", context)
        self.assertEqual(kb_store.source_update({**self.info, "source_commit": head}), (head, ""))

    def test_named_jsonl_overwrite_preserves_latest_other_files_and_git_history(self):
        original = self.jsonl.read_bytes()
        latest_info = {**self.info, "source_commit": self.head}
        kb_store.publish(self.store1, "example", latest_info, self.jsonl)
        first = kb_store.publish(self.store1, "example", self.info, self.jsonl, filename="v1.00.jsonl")
        kb_store.publish(self.store1, "example", self.info, self.jsonl, filename="v2.00.jsonl")
        before = kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")
        self.assertEqual(before["info"], self.info)
        self.assertEqual(before["jsonl"].name, "v1.00.jsonl")
        self.assertEqual(before["jsonl"].stat().st_mode & 0o777, 0o600)
        replacement = original + b'\n'
        self.jsonl.write_bytes(replacement)
        updated = kb_store.publish(self.store1, "example", latest_info, self.jsonl, filename="v1.00.jsonl")
        loaded = kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")
        self.assertEqual(loaded["info"], latest_info)
        self.assertEqual(loaded["jsonl"].read_bytes(), replacement)
        self.assertEqual(before["jsonl"].read_bytes(), original)
        self.assertEqual(kb_store.find_kb(self.config, "example")["jsonl"].read_bytes(), original)
        self.assertEqual(kb_store.find_kb(self.config, "example")["info"], latest_info)
        self.assertEqual(kb_store.find_kb(self.config, "example", filename="v2.00.jsonl")["jsonl"].read_bytes(), original)
        self.assertEqual(kb_store.git(self.store1["url"], "show", f"{first}:example/v1.00.jsonl", text=False).stdout, original)
        self.assertEqual(kb_store.git(self.store1["url"], "diff-tree", "--no-commit-id", "--name-only", "-r", updated).stdout.splitlines(),
                         ["example/v1.00.info.json", "example/v1.00.jsonl"])

    def test_named_lookup_uses_matching_file_and_explicit_store(self):
        kb_store.publish(self.store1, "example", self.info, self.jsonl)
        kb_store.publish(self.store2, "example", self.info, self.jsonl, filename="v1.00.jsonl")
        self.assertEqual(kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")["store"], self.store2)
        kb_store.publish(self.store1, "example", self.info, self.jsonl, filename="v1.00.jsonl")
        self.assertEqual(kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")["store"], self.store1)
        self.assertEqual(kb_store.find_kb(self.config, "example", "second", filename="v1.00.jsonl")["store"], self.store2)

    def test_create_named_kb_from_registration_and_list_each_file(self):
        kb_store.write_config(self.config)
        registration = {**self.info, "source_commit": None}
        kb_store.publish(self.store2, "example", registration, create_only=True)
        with mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl) as rebuild, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["create", "example", "--file", "v1.00.jsonl", "--store", "second"])
        rebuild.assert_called_once_with("example", registration, self.head, self.config)
        loaded = kb_store.find_kb(self.config, "example", "second", filename="v1.00.jsonl")
        self.assertEqual(loaded["info"]["source_commit"], self.head)
        self.assertEqual(loaded["jsonl"].read_bytes(), self.jsonl.read_bytes())
        self.assertEqual(kb_store.find_kb(self.config, "example", "second", download=False)["info"], registration)
        self.assertNotEqual(kb_store.git(self.store2["url"], "cat-file", "-e", "main:example/latest.jsonl", check=False).returncode, 0)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kb_cli.main(["list"])
        self.assertIn(f"example\tsecond\t{self.head[:12]}\tmain\tv1.00.jsonl", output.getvalue())
        self.assertIn("example\tsecond\t未作成\tmain\tlatest.jsonl", output.getvalue())
        with mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl), contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["create", "example", "--store", "second"])
        self.assertEqual(kb_store.find_kb(self.config, "example", "second")["jsonl"].read_bytes(), self.jsonl.read_bytes())
        self.assertEqual(kb_store.find_kb(self.config, "example", "second", filename="v1.00.jsonl")["info"], loaded["info"])

    def test_publish_named_file_from_cli_creates_registration_and_preserves_input(self):
        kb_store.write_config(self.config)
        original = self.jsonl.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["publish", "example", "--file", "検証 v1.00.jsonl", "--store", "second",
                         "--repository-url", str(self.source), "--source-commit", self.base,
                         "--branch", "main", "--jsonl", str(self.jsonl)])
        loaded = kb_store.find_kb(self.config, "example", "second", filename="検証 v1.00.jsonl")
        self.assertEqual(loaded["info"], self.info)
        self.assertEqual(loaded["jsonl"].read_bytes(), original)
        self.assertEqual(self.jsonl.read_bytes(), original)
        self.assertEqual(kb_store.find_kb(self.config, "example", "second", download=False)["info"],
                         {**self.info, "source_commit": None})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kb_cli.main(["list"])
        self.assertIn(f"example\tsecond\t{self.base[:12]}\tmain\t検証 v1.00.jsonl", output.getvalue())

    def test_codex_named_file_checks_its_own_commit(self):
        kb_store.write_config(self.config)
        kb_store.publish(self.store1, "example", self.info, self.jsonl)
        kb_store.publish(self.store1, "example", {**self.info, "source_commit": self.head}, self.jsonl, filename="v1.00.jsonl")
        with mock.patch("builtins.input") as question, \
             mock.patch.object(kb_cli, "rebuild") as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["codex", "example", "--file", "v1.00.jsonl", "--session-only"])
        question.assert_not_called()
        rebuild.assert_not_called()
        self.assertEqual(start.call_args.args[0].name, "v1.00.jsonl")
        self.assertEqual(start.call_args.args[0].read_bytes(), self.jsonl.read_bytes())
        self.assertEqual(start.call_args.args[3], "")

    def test_named_rebuild_updates_selected_file_and_preserves_latest(self):
        original = self.jsonl.read_bytes()
        latest_info = {**self.info, "source_commit": self.head}
        kb_store.publish(self.store1, "example", latest_info, self.jsonl)
        kb_store.publish(self.store1, "example", self.info, self.jsonl, filename="v1.00.jsonl")
        self.jsonl.write_bytes(original + b'\n')
        with mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl) as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.launch(self.args("always", "v1.00.jsonl"), self.config)
        rebuild.assert_called_once_with("example", self.info, self.head, self.config)
        self.assertEqual(start.call_args.args[3], "")
        named = kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")
        self.assertEqual(named["info"]["source_commit"], self.head)
        self.assertEqual(named["jsonl"].read_bytes(), self.jsonl.read_bytes())
        latest = kb_store.find_kb(self.config, "example")
        self.assertEqual(latest["info"], latest_info)
        self.assertEqual(latest["jsonl"].read_bytes(), original)

    def test_named_kb_diff_uses_named_commit_without_publishing(self):
        kb_store.publish(self.store1, "example", {**self.info, "source_commit": self.head}, self.jsonl)
        revision = kb_store.publish(self.store1, "example", self.info, self.jsonl, filename="v1.00.jsonl")
        with mock.patch.object(kb_cli, "rebuild") as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.launch(self.args("never", "v1.00.jsonl"), self.config)
        rebuild.assert_not_called()
        self.assertEqual(start.call_args.args[0].name, "v1.00.jsonl")
        self.assertIn("-old value", start.call_args.args[3])
        self.assertIn("+new value", start.call_args.args[3])
        self.assertEqual(kb_store.find_kb(self.config, "example")["store_revision"], revision)

    def test_missing_named_file_does_not_start_latest_instead(self):
        kb_store.write_config(self.config)
        kb_store.publish(self.store1, "example", self.info, self.jsonl)
        with mock.patch.object(kb_codex, "start_session") as start:
            with self.assertRaisesRegex(ValueError, "v1.00.jsonl"):
                kb_cli.main(["codex", "example", "--file", "v1.00.jsonl", "--session-only"])
        start.assert_not_called()

    def test_file_without_suffix_creates_publishes_and_opens_the_same_jsonl(self):
        kb_store.write_config(self.config)
        kb_store.publish(self.store1, "example", self.info, create_only=True)
        with mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl), contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["create", "example", "--file", "v1.00"])
        loaded = kb_store.find_kb(self.config, "example", filename="v1.00.jsonl")
        self.assertEqual(loaded["jsonl"].read_bytes(), self.jsonl.read_bytes())

        self.jsonl.write_bytes(self.jsonl.read_bytes() + b"\n")
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["publish", "example", "--file", "v1.00",
                         "--repository-url", str(self.source), "--source-commit", self.head,
                         "--branch", "main", "--jsonl", str(self.jsonl)])
        for filename in ("v1.00", "v1.00.jsonl"):
            with self.subTest(filename=filename), \
                 mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
                 contextlib.redirect_stdout(io.StringIO()):
                kb_cli.main(["codex", "example", "--file", filename, "--session-only"])
            self.assertEqual(start.call_args.args[0].name, "v1.00.jsonl")
            self.assertEqual(start.call_args.args[0].read_bytes(), self.jsonl.read_bytes())
        files = kb_store.git(self.store1["url"], "ls-tree", "-r", "--name-only", "main").stdout.splitlines()
        self.assertEqual([path for path in files if "v1.00" in path],
                         ["example/v1.00.info.json", "example/v1.00.jsonl"])

    def test_file_without_suffix_still_rejects_paths_and_empty_names(self):
        for filename in ("../outside", "/tmp/out", "sub/path", "sub\\path", "", ".jsonl"):
            with self.subTest(filename=filename), mock.patch.object(kb_store, "read_config") as config, \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    kb_cli.main(["codex", "example", "--file", filename])
                self.assertEqual(error.exception.code, 2)
                config.assert_not_called()

    def test_filename_cannot_escape_kb_directory_or_overwrite_metadata(self):
        for filename in ("../outside.jsonl", "/tmp/out.jsonl", "sub/path.jsonl", "sub\\path.jsonl", "info.json", ".jsonl", ""):
            with self.subTest(filename=filename), mock.patch.object(kb_store, "default_branch") as branch:
                with self.assertRaises(ValueError):
                    kb_store.publish(self.store1, "example", self.info, self.jsonl, filename=filename)
                with self.assertRaises(ValueError):
                    kb_store.find_kb(self.config, "example", filename=filename)
                branch.assert_not_called()

    def test_config_add_update_remove_and_list_kbs(self):
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["store", "add", "first", self.store1["url"]])
            kb_cli.main(["store", "add", "second", self.store2["url"]])
            kb_cli.main(["store", "add", "first", self.store1["url"], "--branch", "main"])
        self.assertEqual([s["name"] for s in kb_store.read_config()["stores"]], ["first", "second"])
        kb_store.publish(self.store2, "example", self.info, self.jsonl)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kb_cli.main(["list"])
        self.assertIn(f"example\tsecond\t{self.base[:12]}\tmain", output.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["store", "remove", "first"])
        self.assertEqual(kb_store.read_config()["stores"], [self.store2])

    def test_register_prompts_for_name_source_branch_store_and_lists_unbuilt(self):
        with mock.patch("builtins.input", side_effect=["example", str(self.source), "feature/kb", self.store2["url"]]) as prompt, \
             mock.patch.object(kb_cli, "rebuild") as rebuild, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["register"])
        self.assertEqual([call.args[0] for call in prompt.call_args_list],
                         ["KB名: ", "元リポジトリのURL: ", "基準ブランチ [main]: ", "保存先GitリポジトリのURL: "])
        config = kb_store.read_config()
        self.assertEqual(config["stores"], [{"name": "store2", "url": self.store2["url"]}])
        loaded = kb_store.find_kb(config, "example", download=False)
        self.assertEqual(loaded["info"], {"repository_url": str(self.source), "source_commit": None, "branch": "feature/kb"})
        self.assertIsNone(loaded["jsonl"])
        self.assertEqual(kb_store.git(self.store2["url"], "ls-tree", "--name-only", "main:example").stdout.splitlines(), ["info.json"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            kb_cli.main(["list"])
        self.assertIn("example\tstore2\t未作成\tfeature/kb", output.getvalue())
        with mock.patch.object(kb_codex, "start_session") as start:
            with self.assertRaisesRegex(ValueError, "kb create example"):
                kb_cli.main(["codex", "example", "--session-only"])
        start.assert_not_called()
        rebuild.assert_not_called()

    def test_register_uses_existing_store_defaults_and_preserves_build_settings(self):
        config = {**self.config, "build_args": ["--workers", "12"]}
        kb_store.write_config(config)
        with mock.patch("builtins.input", side_effect=[str(self.source), "", ""]) as prompt, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["register", "example"])
        self.assertEqual(prompt.call_count, 3)
        self.assertEqual(kb_store.read_config(), config)
        loaded = kb_store.find_kb(config, "example", download=False)
        self.assertEqual(loaded["store"], self.store1)
        self.assertEqual(loaded["info"]["branch"], "main")

    def test_register_does_not_replace_existing_kb_or_its_metadata(self):
        kb_store.write_config(self.config)
        revision = kb_store.publish(self.store1, "example", self.info, self.jsonl)
        with mock.patch("builtins.input", side_effect=["https://example.com/other.git", "other", self.store1["url"]]), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "登録済み"):
                kb_cli.main(["register", "example"])
        loaded = kb_store.find_kb(self.config, "example")
        self.assertEqual(loaded["info"], self.info)
        self.assertEqual(loaded["store_revision"], revision)
        self.assertEqual(loaded["jsonl"].read_bytes(), self.jsonl.read_bytes())

    def test_register_new_store_name_collision_keeps_existing_entries(self):
        config = {"stores": [{"name": "store2", "url": self.store1["url"]}], "build_args": ["--workers", "12"]}
        kb_store.write_config(config)
        with mock.patch("builtins.input", side_effect=[str(self.source), "main", self.store2["url"]]), \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["register", "example"])
        saved = kb_store.read_config()
        self.assertEqual(saved["stores"], [*config["stores"], {"name": "store2-2", "url": self.store2["url"]}])
        self.assertEqual(saved["build_args"], config["build_args"])

    def test_incomplete_registration_does_not_publish_or_change_config(self):
        for answers, error in (([""], ValueError), (["example", ""], ValueError),
                               (["example", EOFError()], EOFError)):
            with self.subTest(answers=answers), mock.patch("builtins.input", side_effect=answers), \
                 mock.patch.object(kb_store, "publish") as publish:
                with self.assertRaises(error):
                    kb_cli.main(["register"])
                publish.assert_not_called()
                self.assertFalse(kb_store.CONFIG.exists())

    def test_create_uses_requested_store_and_registered_branch(self):
        kb_store.git(self.source, "branch", "kb-source", self.base)
        kb_store.write_config(self.config)
        first_revision = kb_store.publish(self.store1, "example", self.info, self.jsonl)
        selected_info = {**self.info, "branch": "kb-source"}
        kb_store.publish(self.store2, "example", selected_info, self.jsonl)
        with mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl) as rebuild, \
             mock.patch.object(kb_codex, "start_session") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["create", "example", "--store", "second"])
        rebuild.assert_called_once_with("example", selected_info, self.base, self.config)
        start.assert_not_called()
        self.assertEqual(kb_store.find_kb(self.config, "example", "first")["store_revision"], first_revision)

    def test_create_failure_leaves_registration_unbuilt(self):
        kb_store.write_config(self.config)
        info = {**self.info, "source_commit": None}
        revision = kb_store.publish(self.store1, "example", info, create_only=True)
        with mock.patch.object(kb_cli, "rebuild", side_effect=RuntimeError("generation failed")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                kb_cli.main(["create", "example"])
        loaded = kb_store.find_kb(self.config, "example", download=False)
        self.assertEqual(loaded["info"], info)
        self.assertEqual(loaded["store_revision"], revision)

    def args(self, rebuild="ask", filename="latest.jsonl"):
        return SimpleNamespace(name="example", store=None, rebuild=rebuild, workspace=str(self.root),
                               no_yolo=False, session_only=True, app=False, prompt=None, file=filename)

    def test_decline_rebuild_sends_diff_to_new_session_without_publishing(self):
        revision = kb_store.publish(self.store2, "example", self.info, self.jsonl)
        with mock.patch("builtins.input", return_value="n") as question, \
             mock.patch.object(kb_cli, "rebuild") as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kb_cli.launch(self.args(), self.config), "new-id")
        question.assert_called_once()
        rebuild.assert_not_called()
        self.assertIn("+new value", start.call_args.args[3])
        self.assertEqual(start.call_args.args[0].read_bytes(), self.jsonl.read_bytes())
        self.assertEqual(kb_store.find_kb(self.config, "example")["store_revision"], revision)

    def test_accept_rebuild_publishes_to_selected_origin_then_starts_new_kb(self):
        kb_store.publish(self.store2, "example", self.info, self.jsonl)
        with mock.patch("builtins.input", return_value="y"), \
             mock.patch.object(kb_cli, "rebuild", return_value=self.jsonl) as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.launch(self.args(), self.config)
        rebuild.assert_called_once_with("example", self.info, self.head, self.config)
        loaded = kb_store.find_kb(self.config, "example")
        self.assertEqual(loaded["store"]["name"], "second")
        self.assertEqual(loaded["info"]["source_commit"], self.head)
        self.assertEqual(start.call_args.args[3], "")

    def test_current_kb_does_not_ask_or_rebuild(self):
        kb_store.publish(self.store1, "example", {**self.info, "source_commit": self.head}, self.jsonl)
        with mock.patch("builtins.input") as question, mock.patch.object(kb_cli, "rebuild") as rebuild, \
             mock.patch.object(kb_codex, "start_session", return_value="new-id") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.launch(self.args(), self.config)
        question.assert_not_called()
        rebuild.assert_not_called()
        self.assertEqual(start.call_args.args[3], "")

    def test_rebuild_failure_does_not_publish_or_start(self):
        revision = kb_store.publish(self.store1, "example", self.info, self.jsonl)
        with mock.patch.object(kb_cli, "rebuild", side_effect=RuntimeError("generation failed")), \
             mock.patch.object(kb_codex, "start_session") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "generation failed"):
                kb_cli.launch(self.args("always"), self.config)
        start.assert_not_called()
        self.assertEqual(kb_store.find_kb(self.config, "example")["store_revision"], revision)

    def test_rebuild_runs_existing_v2_builder_and_publishes_result(self):
        self.check_builder_flow(first_build=False)

    def test_register_then_create_runs_v2_builder_and_publishes_result(self):
        self.check_builder_flow(first_build=True)

    def check_builder_flow(self, *, first_build):
        requests = []
        output_blob = {"type": "compaction", "id": "rebuilt", "encrypted_content": "new-opaque"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                events = [
                    {"type": "response.output_item.done", "item": output_blob},
                    {"type": "response.completed", "response": {"usage": {"output_tokens": 3000}}},
                ]
                data = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        key = self.root / "client.key"
        key.write_text("synthetic-key")
        repo_map = self.root / "map.txt"
        repo_map.write_text("code.py contains a value\n")
        config = {**self.config, "build_args": ["--origin", f"http://127.0.0.1:{server.server_port}",
                                              "--key-file", str(key), "--repo-map", str(repo_map)]}
        if first_build:
            kb_store.write_config(config)
            with mock.patch("builtins.input", side_effect=[str(self.source), "main", self.store1["url"]]), \
                 contextlib.redirect_stdout(io.StringIO()):
                kb_cli.main(["register", "example"])
        else:
            kb_store.publish(self.store1, "example", self.info, self.jsonl)
        original_run = subprocess.run
        minted = self.root / ".codex/sessions/2026/09/17/rollout-test-rebuilt-id.jsonl"

        def run(command, **kwargs):
            if len(command) > 1 and str(command[1]).endswith("kb_fork_mint.py"):
                state_root = Path(kwargs["env"]["KB_REPOMAP_HOME"])
                state = json.loads((state_root / "example/state.json").read_text())
                self.assertEqual(state["commit"], self.head)
                self.assertEqual(state["blobs"], [output_blob])
                minted.parent.mkdir(parents=True)
                minted.write_text(json.dumps({"type": "session_meta", "payload": {"id": "rebuilt-id"}}) + "\n"
                                  + json.dumps({"type": "response_item", "payload": state["blobs"][0]}) + "\n")
                (state_root / "kb-session").write_text("rebuilt-id\n")
                return SimpleNamespace(returncode=0)
            return original_run(command, **kwargs)

        with mock.patch.object(kb_cli, "BUILD_ROOT", self.root / "builds"), \
             mock.patch.object(Path, "home", return_value=self.root), \
             mock.patch.object(subprocess, "run", side_effect=run), \
             mock.patch.object(kb_codex, "start_session", return_value="started") as start, \
             contextlib.redirect_stdout(io.StringIO()):
            if first_build:
                kb_cli.main(["create", "example"])
            else:
                kb_cli.launch(self.args("always"), config)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], "/_pool/rr/responses")
        self.assertEqual(requests[0][1]["input"][-1], {"type": "compaction_trigger"})
        self.assertIn("new value", requests[0][1]["input"][0]["content"][0]["text"])
        latest = kb_store.find_kb(config, "example")
        self.assertEqual(latest["info"]["source_commit"], self.head)
        self.assertEqual(latest["jsonl"].read_bytes(), minted.read_bytes())
        if first_build:
            start.assert_not_called()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                kb_cli.main(["list"])
            self.assertIn(self.head[:12], output.getvalue())
            self.assertNotIn("未作成", output.getvalue())
        else:
            self.assertEqual(start.call_args.args[3], "")


class CodexHandoffTest(unittest.TestCase):
    def test_import_assigns_fresh_ids_without_changing_any_response_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "latest.jsonl"
            source.write_bytes(b'{"type":"session_meta","payload":{"id":"original","session_id":"original"}}\n'
                               + b'{"type":"response_item", "payload":{"type":"compaction","encrypted_content":"opaque","unknown":true}}\n' * 35)
            original = source.read_bytes()
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(root / "codex-test")}):
                first_id = kb_codex.import_template(source)
                second_id = kb_codex.import_template(source)
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(source.read_bytes(), original)
            for session_id in (first_id, second_id):
                imported = next((root / "codex-test").rglob(f"*{session_id}.jsonl"))
                first, _, rest = imported.read_bytes().partition(b"\n")
                meta = json.loads(first)["payload"]
                self.assertEqual(meta["id"], session_id)
                self.assertEqual(meta["session_id"], session_id)
                self.assertEqual(rest, original.partition(b"\n")[2])
                self.assertEqual(imported.stat().st_mode & 0o777, 0o600)

    def test_diff_is_submitted_after_fork_with_prompt(self):
        with mock.patch.object(kb_codex, "CodexAppServer") as constructor:
            server = constructor.return_value.__enter__.return_value
            server.fork.return_value = "fork-id"
            sid = kb_codex.start_session(Path("latest.jsonl"), Path("/work"), "example", "EXACT DIFF",
                                         full_access=False, prompt="Use the patch")
        self.assertEqual(sid, "fork-id")
        self.assertEqual(server.method_calls[:2], [
            mock.call.fork(Path("latest.jsonl"), Path("/work"), False),
            mock.call.run_turn("fork-id", "EXACT DIFF\n\nUse the patch", Path("/work")),
        ])

    def test_noninteractive_choice_is_explicit_and_eof_declines(self):
        with mock.patch("builtins.input") as question:
            self.assertTrue(kb_cli.should_rebuild("always"))
            self.assertFalse(kb_cli.should_rebuild("never"))
        question.assert_not_called()
        with mock.patch("builtins.input", side_effect=EOFError), contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(kb_cli.should_rebuild("ask"))

    def test_rebuild_uses_isolated_state_and_existing_generator(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_before = dict(os.environ)
            build_root = root / "builds"
            rollout = root / ".codex/sessions/2026/09/17/rollout-test-fake-id.jsonl"
            rollout.parent.mkdir(parents=True)
            rollout.write_text('test')
            calls = []
            def run(command, *, env, check):
                calls.append((command, env))
                if command[1].endswith("kb_fork_mint.py"):
                    (Path(env["KB_REPOMAP_HOME"]) / "kb-session").write_text("fake-id\n")
            with mock.patch.object(kb_cli, "BUILD_ROOT", build_root), \
                 mock.patch.object(Path, "home", return_value=root), \
                 mock.patch.object(kb_cli.subprocess, "run", side_effect=run):
                result = kb_cli.rebuild("example", {"repository_url": "source"}, "a" * 40,
                                       {"build_args": ["--workers", "12"]})
            self.assertEqual(result, rollout)
            self.assertIn("--no-mint", calls[0][0])
            self.assertEqual(calls[0][0][-3:-1], ["--workspace", str(Path(calls[0][1]["KB_REPOMAP_HOME"]) / "repos")])
            self.assertEqual(calls[0][0][calls[0][0].index("--ref") + 1], "a" * 40)
            self.assertEqual(dict(os.environ), env_before)
