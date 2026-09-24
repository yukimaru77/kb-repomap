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
from kb_items import dump_items, guidance_item, load_session_items


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
            opener = stack.enter_context(mock.patch.object(kb_remote.urllib.request, "build_opener"))

            def receive(request, **kwargs):
                bound.extend(json.loads(request.data)["items"])
                return io.BytesIO(b'{"snapshot_id":"fixture","item_count":3}')

            opener.return_value.open.side_effect = receive
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
            kb_cli.main(argv)
        injected = [item for call in server.request.call_args_list
                    if call.args[0] == "thread/inject_items" for item in call.args[1]["items"]]
        server.run_turn.assert_not_called()
        return injected, bound

    def test_all_launch_paths_attach_exact_store_dev_once_without_changing_snapshot(self):
        developer = "  Sources: paper/paper.md; references/; official/code/.\nPreserve exact citations.\n\n"
        self.set_developer(developer)
        dev_item = {"type": "message", "role": "developer",
                    "content": [{"type": "input_text", "text": developer}]}
        original = self.snapshot.read_bytes()
        loaded = kb_store.find_kb(self.config, "example")
        for remote in (False, True):
            for native in (False, True):
                with self.subTest(remote=remote, native=native):
                    injected, bound = self.launch_items(remote=remote, native=native)
                    self.assertEqual(injected + bound, [guidance_item(), dev_item, *self.blobs])
                    if remote:
                        self.assertIn(dev_item, bound)
                        self.assertNotIn(dev_item, injected)
                    else:
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
                    self.assertEqual(injected + bound, [guidance_item(), *self.blobs])

    def test_loader_uses_explicit_text_only_and_does_not_read_adjacent_dev(self):
        (self.snapshot.parent / "dev.txt").write_text("Do not auto-import adjacent files.\n")
        original = self.snapshot.read_bytes()
        for developer in (None, "", "\n \t"):
            with self.subTest(developer=developer):
                self.assertEqual(load_session_items(self.snapshot, developer_text=developer),
                                 [guidance_item(), *self.blobs])
        self.assertEqual(self.snapshot.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
