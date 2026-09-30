import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_cli
import kb_codex
import kb_native
import kb_native_local
import kb_remote
import kb_store
from kb_items import DEFAULT_DEVELOPER_TEXT, developer_item, dump_items, load_session_items


class StoreDeveloperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patches = contextlib.ExitStack()
        self.addCleanup(patches.close)
        patches.enter_context(mock.patch.object(kb_store, "CACHE", self.root / "cache"))
        patches.enter_context(mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        }))
        self.work = self.root / "store-work"
        kb_store.run(["git", "init", "-q", "-b", "main", str(self.work)])
        (self.work / "README.md").write_text("Fixture KB store\n")
        kb_store.git(self.work, "add", ".")
        kb_store.git(self.work, "commit", "-qm", "Initialize")
        origin = self.root / "store.git"
        kb_store.run(["git", "clone", "--bare", "--quiet", str(self.work), str(origin)])
        kb_store.git(self.work, "remote", "add", "origin", str(origin))
        self.store = {"name": "research", "url": str(origin)}
        self.config = {"stores": [self.store]}
        self.workspace = self.root / "source-workspace"
        self.workspace.mkdir()
        (self.workspace / "dev.txt").write_text("Do not import source workspace instructions.\n")
        self.info = {"repository_url": str(self.workspace), "source_commit": "a" * 40,
                     "branch": "main"}
        self.snapshot = self.root / "input.json"
        self.blobs = [{"type": "compaction", "encrypted_content": "opaque",
                       "future": {"preserve": [1, "value"]}}]
        dump_items(self.snapshot, self.blobs)
        kb_store.publish(self.store, "example", self.info, self.snapshot)

    def set_developer(self, text):
        kb_store.git(self.work, "pull", "--quiet", "--ff-only", "origin", "main")
        path = self.work / "example" / "dev.txt"
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
        kb_store.git(self.work, "add", "--", "example/dev.txt")
        kb_store.git(self.work, "commit", "-qm", "Update developer instructions")
        kb_store.git(self.work, "push", "--quiet", "origin", "main")

    def test_publish_commits_developer_and_snapshot_together(self):
        before = kb_store.git(self.store["url"], "rev-parse", "HEAD").stdout.strip()
        developer = ".\n└── paper/ (Original paper and Japanese translation)\n"
        replacement = [{**self.blobs[0], "encrypted_content": "new-opaque"}]
        dump_items(self.snapshot, replacement)
        revision = kb_store.publish(self.store, "example", self.info, self.snapshot,
                                    developer_text=developer)
        changed = kb_store.git(self.store["url"], "diff", "--name-only", before, revision).stdout.splitlines()
        self.assertEqual(changed, ["example/dev.txt", "example/latest.json"])
        self.assertEqual(kb_store.git(self.store["url"], "rev-list", "--count",
                                     f"{before}..{revision}").stdout.strip(), "1")
        loaded = kb_store.find_kb(self.config, "example")
        self.assertEqual(loaded["developer_text"], developer)
        self.assertEqual(loaded["jsonl"].read_bytes(), self.snapshot.read_bytes())
        self.assertEqual(kb_store.publish(self.store, "example", self.info, self.snapshot,
                                         developer_text=developer), revision)

    def test_default_developer_is_written_only_when_absent_in_the_same_commit(self):
        before = kb_store.git(self.store["url"], "rev-parse", "HEAD").stdout.strip()
        dump_items(self.snapshot, [{**self.blobs[0], "encrypted_content": "new-opaque"}])
        revision = kb_store.publish(self.store, "example", self.info, self.snapshot,
                                    default_developer_text=DEFAULT_DEVELOPER_TEXT)
        changed = kb_store.git(self.store["url"], "diff", "--name-only", before, revision).stdout.splitlines()
        self.assertEqual(changed, ["example/dev.txt", "example/latest.json"])
        self.assertEqual(kb_store.git(self.store["url"], "rev-list", "--count",
                                     f"{before}..{revision}").stdout.strip(), "1")
        self.assertEqual(kb_store.find_kb(self.config, "example")["developer_text"], DEFAULT_DEVELOPER_TEXT)
        self.set_developer("Custom notes.\n")
        dump_items(self.snapshot, [{**self.blobs[0], "encrypted_content": "newer-opaque"}])
        kb_store.publish(self.store, "example", self.info, self.snapshot,
                         default_developer_text=DEFAULT_DEVELOPER_TEXT)
        loaded = kb_store.find_kb(self.config, "example")
        self.assertEqual(loaded["developer_text"], "Custom notes.\n")
        self.assertEqual(json.loads(loaded["jsonl"].read_text())[0]["encrypted_content"], "newer-opaque")

    def source_repo(self):
        source = self.root / "source"
        kb_store.run(["git", "init", "-q", "-b", "main", str(source)])
        (source / "gateway").mkdir()
        (source / "gateway" / "main.go").write_text("package main\n")
        (source / "README.md").write_text("readme\n")
        (source / ".gitignore").write_text("build/\n")
        (source / "build").mkdir()
        (source / "build" / "ignored.bin").write_text("x")
        kb_store.git(source, "add", ".")
        kb_store.git(source, "commit", "-qm", "Initial")
        commit = kb_store.git(source, "rev-parse", "HEAD").stdout.strip()
        (source / "later.txt").write_text("after the KB commit\n")
        kb_store.git(source, "add", ".")
        kb_store.git(source, "commit", "-qm", "Later")
        return source, commit

    def expected_default(self, source, commit, files=False):
        if files:
            return (DEFAULT_DEVELOPER_TEXT + "\n"
                    f"## Repository structure with files ({source}@{commit[:12]})\n\n"
                    ".\n  .gitignore\n  README.md\n  gateway/\n    main.go\n")
        return (DEFAULT_DEVELOPER_TEXT + "\n"
                f"## Repository structure ({source}@{commit[:12]})\n\n"
                ".\n  gateway/\n")

    def test_create_writes_default_dev_with_tree_when_absent_and_keeps_existing(self):
        source, commit = self.source_repo()
        loaded = kb_store.find_kb(self.config, "example")
        loaded = {**loaded, "info": {**loaded["info"], "repository_url": str(source)}}
        self.assertIsNone(loaded["developer_text"])
        rebuilt = self.root / "rebuilt" / "example" / "kb.json"
        rebuilt.parent.mkdir(parents=True)
        dump_items(rebuilt, self.blobs)
        with mock.patch.object(kb_cli, "rebuild", return_value=rebuilt), \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.create_kb("example", loaded, commit, self.config)
            self.assertEqual(kb_store.find_kb(self.config, "example")["developer_text"],
                             self.expected_default(source, commit))
            self.set_developer("Keep this.\n")
            kb_cli.create_kb("example", {**kb_store.find_kb(self.config, "example"), "info": loaded["info"]},
                             commit, self.config)
        after = kb_store.find_kb(self.config, "example")
        self.assertEqual(after["developer_text"], "Keep this.\n")

    def test_create_uses_the_build_clone_and_falls_back_to_guidance_only(self):
        source, commit = self.source_repo()
        info = {**self.info, "repository_url": "https://example.invalid/unreachable.git"}
        build = self.root / "build" / "repos" / "example"
        kb_store.run(["git", "clone", "--quiet", str(source), str(build)])
        text = kb_cli.default_developer_text(info, commit, build, include_files=True)
        self.assertIn(f"## Repository structure with files (https://example.invalid/unreachable.git@{commit[:12]})",
                      text)
        self.assertIn("  README.md\n", text)
        self.assertNotIn("later.txt", text)
        self.assertNotIn("ignored.bin", text)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(kb_cli.default_developer_text(info, commit, self.root / "missing"),
                             DEFAULT_DEVELOPER_TEXT)
        self.assertIn("案内文のみ", err.getvalue())

    def test_dev_init_publishes_tree_at_source_commit_and_needs_force_to_overwrite(self):
        source, commit = self.source_repo()
        info = {**self.info, "repository_url": str(source), "source_commit": commit}
        kb_store.publish(self.store, "example", info, self.snapshot)
        with mock.patch.object(kb_store, "read_config", return_value=self.config), \
             contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["dev-init", "example"])
            self.assertEqual(kb_store.find_kb(self.config, "example")["developer_text"],
                             self.expected_default(source, commit))
            self.set_developer("Custom.\n")
            with self.assertRaisesRegex(ValueError, "--force"):
                kb_cli.main(["dev-init", "example"])
            self.assertEqual(kb_store.find_kb(self.config, "example")["developer_text"], "Custom.\n")
            kb_cli.main(["dev-init", "example", "--force", "--tree-files"])
        self.assertEqual(kb_store.find_kb(self.config, "example")["developer_text"],
                         self.expected_default(source, commit, files=True))

    def test_create_cli_passes_tree_files(self):
        loaded = {"info": {"repository_url": "repo", "branch": "main"}, "store": self.store}
        for argv, expected in ((["create", "example"], False), (["create", "example", "--tree-files"], True)):
            with self.subTest(argv=argv), \
                 mock.patch.object(kb_store, "read_config", return_value=self.config), \
                 mock.patch.object(kb_store, "find_kb", return_value=loaded), \
                 mock.patch.object(kb_store, "source_head", return_value="d" * 40), \
                 mock.patch.object(kb_cli, "previous_kb", return_value=None), \
                 mock.patch.object(kb_cli, "create_kb") as create:
                kb_cli.main(argv)
            self.assertIs(create.call_args.kwargs["tree_files"], expected)

    def test_publish_only_developer_preserves_all_other_git_objects_and_is_idempotent(self):
        before = kb_store.git(self.store["url"], "rev-parse", "HEAD").stdout.strip()
        original_tree = kb_store.git(self.store["url"], "ls-tree", "-r", before).stdout.splitlines()
        developer = "  .\n└── translation/ (日本語訳)\n\n"
        revision = kb_store.publish_developer(self.store, "example", developer)
        published_tree = kb_store.git(self.store["url"], "ls-tree", "-r", revision).stdout.splitlines()
        self.assertEqual(original_tree, [line for line in published_tree
                                       if not line.endswith("\texample/dev.txt")])
        raw = kb_store.git(self.store["url"], "show", f"{revision}:example/dev.txt", text=False).stdout
        self.assertEqual(raw, developer.encode("utf-8"))
        self.assertEqual(kb_store.publish_developer(self.store, "example", developer), revision)
        edited = kb_store.publish_developer(self.store, "example", developer + "Updated description.\n")
        self.assertEqual(kb_store.git(self.store["url"], "diff", "--name-only", revision, edited).stdout,
                         "example/dev.txt\n")

    def test_developer_publication_rejects_blank_text_and_unregistered_names(self):
        before = kb_store.git(self.store["url"], "rev-parse", "HEAD").stdout.strip()
        for text in ("", " \n\t"):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "空でない"):
                kb_store.publish_developer(self.store, "example", text)
            with self.subTest(bundled=text), self.assertRaisesRegex(ValueError, "空でない"):
                kb_store.publish(self.store, "example", self.info, self.snapshot, developer_text=text)
        with self.assertRaisesRegex(ValueError, "未登録"):
            kb_store.publish_developer(self.store, "unregistered", "Source tree\n")
        self.assertEqual(kb_store.git(self.store["url"], "rev-parse", "HEAD").stdout.strip(), before)

    def test_add_edit_delete_are_revision_scoped_and_publish_preserves_dev(self):
        original = self.snapshot.read_bytes()
        absent = kb_store.find_kb(self.config, "example")
        self.assertIsNone(absent["developer_text"])
        first_text = "  Inspect paper/paper.md and references/ before answering.\n\n"
        self.set_developer(first_text)
        first = kb_store.find_kb(self.config, "example")
        self.assertEqual(first["developer_text"], first_text)
        self.assertEqual(first["jsonl"].read_bytes(), original)
        self.assertIsNone(kb_store.find_kb(self.config, "example", download=False)["developer_text"])

        second_text = "Use official/code/ for exact implementation details.\n"
        self.set_developer(second_text)
        second = kb_store.find_kb(self.config, "example")
        self.assertEqual(second["developer_text"], second_text)
        self.assertNotEqual(second["store_revision"], first["store_revision"])
        self.assertNotEqual(second["jsonl"], first["jsonl"])
        self.assertEqual(first["developer_text"], first_text)
        self.assertEqual(first["jsonl"].read_bytes(), original)
        self.assertEqual(kb_store.git(self.store["url"], "show",
                                     f"{first['store_revision']}:example/dev.txt").stdout, first_text)

        replacement = [{**self.blobs[0], "encrypted_content": "updated-opaque"}]
        dump_items(self.snapshot, replacement)
        kb_store.publish(self.store, "example", self.info, self.snapshot)
        published = kb_store.find_kb(self.config, "example")
        self.assertEqual(published["developer_text"], second_text)
        self.assertEqual(published["jsonl"].read_bytes(), self.snapshot.read_bytes())
        self.assertEqual(json.loads(published["jsonl"].read_text()), replacement)
        self.assertEqual(second["jsonl"].read_bytes(), original)

        self.set_developer(None)
        deleted = kb_store.find_kb(self.config, "example")
        self.assertIsNone(deleted["developer_text"])
        self.assertEqual(deleted["jsonl"].read_bytes(), published["jsonl"].read_bytes())
        self.assertEqual(published["developer_text"], second_text)

    def launch_items(self, *, remote=False, native=False):
        bound = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(kb_store, "read_config", return_value=self.config))
            stack.enter_context(mock.patch.object(kb_store, "source_update",
                                                 return_value=(self.info["source_commit"], "")))
            constructor = stack.enter_context(mock.patch.object(kb_codex, "CodexAppServer"))
            server = constructor.return_value.__enter__.return_value
            server.start.return_value = "new-thread"
            stack.enter_context(mock.patch.object(kb_codex, "config_flags", return_value=[]))
            stack.enter_context(mock.patch.object(kb_remote, "pool_configuration",
                                                 return_value=("http://127.0.0.1:12345/_pool/rr", "test-key")))
            # kb's own proxy receives the remote items; capture what it would insert.
            stack.enter_context(mock.patch.dict(os.environ, {"KB_REMOTE_MODE": "provider"}))
            def start_proxy(remote, items, on_request=None):
                bound.extend(items)
                return mock.MagicMock(url="http://127.0.0.1:1", port=1), None

            stack.enter_context(mock.patch.object(kb_native, "start_proxy", side_effect=start_proxy))
            stack.enter_context(mock.patch.object(kb_native, "proxy_overrides", return_value=[]))
            stack.enter_context(mock.patch.object(kb_native.proxy, "run_client", return_value=0))
            bindings = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.object(kb_native.proxy, "BINDINGS", Path(bindings)))
            stack.enter_context(mock.patch.object(kb_native, "remote_command",
                                                 return_value=(["codex", "exec", "hello"], {})))
            stack.enter_context(mock.patch.object(kb_native_local, "command",
                                                 return_value=["codex", "exec", "resume", "new-thread", "hello"]))
            stack.enter_context(mock.patch.object(kb_native_local, "seed_overrides", return_value=[]))
            stack.enter_context(mock.patch.object(kb_native.os, "chdir"))
            stack.enter_context(mock.patch.object(kb_native.os, "execvp"))
            stack.enter_context(mock.patch.object(kb_native.os, "execvpe"))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            options = ["--workspace", str(self.workspace)] + (["--remote"] if remote else [])
            argv = (["example", *options, "codex", "exec", "hello"] if native else
                    ["codex", "example", *options, "--session-only"])
            try:
                kb_cli.main(argv)
            except SystemExit as exit_:
                self.assertEqual(exit_.code, 0)
        injected = [item for call in server.request.call_args_list
                    if call.args[0] == "thread/inject_items" for item in call.args[1]["items"]]
        server.run_turn.assert_not_called()
        return injected, bound

    def test_all_launch_paths_attach_exact_store_dev_once_without_changing_snapshot(self):
        developer = "  Sources: paper/paper.md; references/; official/code/.\nPreserve exact citations.\n\n"
        self.set_developer(developer)
        dev_item = {"type": "message", "role": "developer",
                    "content": [{"type": "input_text", "text": developer}]}
        seed = developer_item(kb_codex.REMOTE_SEED_TEXT)
        original = self.snapshot.read_bytes()
        loaded = kb_store.find_kb(self.config, "example")
        for remote in (False, True):
            for native in (False, True):
                with self.subTest(remote=remote, native=native):
                    injected, bound = self.launch_items(remote=remote, native=native)
                    if remote:
                        # The proxy inserts every KB item, dev.txt after the blobs.
                        self.assertEqual(bound, [*self.blobs, dev_item])
                        # A seeded legacy thread persists only a neutral marker.
                        self.assertEqual(injected, [] if native else [seed])
                    else:
                        self.assertEqual(injected, [*self.blobs, dev_item])
                        self.assertEqual(bound, [])
                    self.assertEqual(loaded["jsonl"].read_bytes(), original)
                    self.assertEqual(self.snapshot.read_bytes(), original)

    def test_missing_or_whitespace_store_dev_never_imports_workspace_dev(self):
        for developer in (None, " \n\t\n"):
            with self.subTest(developer=developer):
                if developer is not None:
                    self.set_developer(developer)
                for remote in (False, True):
                    injected, bound = self.launch_items(remote=remote)
                    kb = [item for item in injected + bound
                          if item != developer_item(kb_codex.REMOTE_SEED_TEXT)]
                    self.assertEqual(kb, self.blobs)

    def test_loader_uses_explicit_text_only_and_does_not_read_adjacent_dev(self):
        (self.snapshot.parent / "dev.txt").write_text("Do not auto-import adjacent files.\n")
        original = self.snapshot.read_bytes()
        for developer in (None, "", "\n \t"):
            with self.subTest(developer=developer):
                self.assertEqual(load_session_items(self.snapshot, developer_text=developer), self.blobs)
        self.assertEqual(self.snapshot.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
