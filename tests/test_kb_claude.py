import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_claude
import kb_store


class ClaudeContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(kb_store, "CACHE", self.root / "cache").start()
        mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}).start()
        repo = self.root / "store"
        kb_store.run(["git", "init", "-q", "-b", "main", str(repo)])
        (repo / "example").mkdir()
        (repo / "example" / "info.json").write_text(json.dumps({"source_commit": "a"}))
        (repo / "example" / "dev.txt").write_text("記録の場所")
        (repo / "example" / "latest.json").write_text(json.dumps([{
            "type": "compaction", "id": "blob", "encrypted_content": "opaque"
        }]))
        raw = repo / "example" / "decrypt" / "latest" / "01-blob" / "raw.txt"
        raw.parent.mkdir(parents=True)
        raw.write_text("復号された知識")
        kb_store.git(repo, "add", ".")
        kb_store.git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
        bare = self.root / "store.git"
        kb_store.run(["git", "clone", "--bare", str(repo), str(bare)])
        self.loaded = {"store": {"name": "test", "url": str(bare)}, "developer_text": "記録の場所"}

    def test_build_context_uses_dev_and_selected_raw_text(self):
        text = kb_claude.build_context(self.loaded, "example", "latest.json")
        self.assertIn("記録の場所", text)
        self.assertIn("復号された知識", text)

    def test_dev_notes_follow_all_decrypted_material(self):
        text = kb_claude.build_context(self.loaded, "example", "latest.json")
        material = text.index("## KB section 1/1\n> " + kb_claude.SCOPE_NOTE + "\n\n復号された知識")
        notes = text.index("## KB developer notes\n\n記録の場所")
        self.assertLess(material, notes)
        self.assertLess(text.index("復号された知識"), notes)
        self.assertTrue(text.endswith("記録の場所\n"))

    def test_no_dev_means_no_notes_section(self):
        for developer in (None, "", " \n"):
            with self.subTest(developer=developer):
                text = kb_claude.build_context({**self.loaded, "developer_text": developer},
                                               "example", "latest.json")
                self.assertNotIn("## KB developer notes", text)
                self.assertIn("復号された知識", text)

    def test_missing_decrypt_is_rejected(self):
        empty_repo = self.root / "empty"
        kb_store.run(["git", "init", "-q", "-b", "main", str(empty_repo)])
        (empty_repo / "example").mkdir()
        (empty_repo / "example" / "info.json").write_text(json.dumps({"source_commit": "a"}))
        kb_store.git(empty_repo, "add", ".")
        kb_store.git(empty_repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture")
        empty_bare = self.root / "empty.git"
        kb_store.run(["git", "clone", "--bare", str(empty_repo), str(empty_bare)])
        self.loaded["store"] = {"name": "empty", "url": str(empty_bare)}
        with self.assertRaisesRegex(ValueError, "kb decrypt example"):
            kb_claude.build_context(self.loaded, "example", "latest.json")

    def test_start_executes_claude_with_a_new_session_and_context(self):
        args = type("Args", (), {
            "name": "example", "file": "latest.json", "store": "test",
            "workspace": str(self.root), "claude_args": ["--model", "sonnet"],
        })()
        with mock.patch.object(kb_claude.os, "execvpe", side_effect=SystemExit) as execvp, \
             mock.patch.object(kb_claude.os, "chdir") as chdir, \
             self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        chdir.assert_called_once_with(self.root.resolve())
        command = execvp.call_args.args[1]
        self.assertEqual(command[0:2], ["claude", "--resume"])
        self.assertNotIn("--append-system-prompt", command)
        transcript = next((self.root / "claude" / "projects").rglob(f"{command[2]}.jsonl"))
        self.assertIn("復号された知識", json.loads(transcript.read_text())["message"]["content"])
        self.assertIn("--model", command)

    def test_relative_claude_config_dir_is_resolved_before_chdir(self):
        relative = os.path.relpath(self.root / "relative-config", Path.cwd())
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": "test",
                                 "workspace": str(self.root), "claude_args": []})()
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": relative}), \
             mock.patch.object(kb_claude.os, "execvpe", side_effect=SystemExit) as execvpe, \
             mock.patch.object(kb_claude.os, "chdir"), self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        env = execvpe.call_args.args[2]
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str((self.root / "relative-config").resolve()))
        self.assertTrue(list((self.root / "relative-config" / "projects").rglob("*.jsonl")))

    def test_failed_exec_removes_new_seed(self):
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": "test",
                                 "workspace": str(self.root), "claude_args": []})()
        with mock.patch.object(kb_claude.os, "execvpe", side_effect=OSError("missing")), \
             mock.patch.object(kb_claude.os, "chdir"), self.assertRaises(OSError):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        self.assertEqual(list((self.root / "claude" / "projects").rglob("*.jsonl")), [])

    def test_continue_resolves_and_verifies_exact_session(self):
        from kb_claude_history import seed_new_session
        sid = "11111111-2222-4333-8444-555555555555"
        seed_new_session(self.root, kb_claude.build_context(self.loaded, "example", "latest.json"), session_id=sid)
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": "test",
                                 "workspace": str(self.root), "claude_args": ["--continue", "--model", "sonnet"]})()
        with mock.patch("kb_resume.claude_latest_session", return_value=sid), \
             mock.patch.object(kb_claude.os, "execvpe", side_effect=SystemExit) as execute, \
             mock.patch.object(kb_claude.os, "chdir"), self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        self.assertEqual(execute.call_args.args[1], ["claude", "--resume", sid, "--model", "sonnet"])

    def test_unverifiable_existing_session_does_not_launch_claude(self):
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": "test",
                                 "workspace": str(self.root),
                                 "claude_args": ["--resume", "11111111-2222-4333-8444-555555555555"]})()
        with mock.patch.object(kb_claude.os, "execvpe") as execute, self.assertRaises(ValueError):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        execute.assert_not_called()

    def test_no_session_persistence_does_not_write_plaintext_history(self):
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": "test",
                                 "workspace": str(self.root),
                                 "claude_args": ["--no-session-persistence", "-p", "hi"]})()
        with mock.patch.object(kb_claude.os, "execvpe") as execute, \
             self.assertRaisesRegex(ValueError, "no-session-persistence"):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        execute.assert_not_called()
        self.assertEqual(list((self.root / "claude").rglob("*.jsonl")), [])

    def test_resume_reattaches_the_kb_without_a_new_session_id(self):
        from kb_claude_history import seed_new_session
        from kb_claude import build_context
        sid = "11111111-2222-4333-8444-555555555555"
        seed_new_session(self.root, build_context(self.loaded, "example", "latest.json"), session_id=sid)
        for resume in (["--resume", sid], ["-r", sid]):
            args = type("Args", (), {
                "name": "example", "file": "latest.json", "store": "test",
                "workspace": str(self.root), "claude_args": [*resume, "-p", "hi"],
            })()
            with mock.patch.object(kb_claude.os, "execvpe", side_effect=SystemExit) as execvp, \
                 mock.patch.object(kb_claude.os, "chdir"), \
                 self.assertRaises(SystemExit):
                kb_claude.start(args, {"stores": [self.loaded["store"]]})
            command = execvp.call_args.args[1]
            self.assertNotIn("--session-id", command)  # Claude Code rejects it with --resume/--continue
            self.assertEqual(command[1:], [*resume, "-p", "hi"])

    def test_fork_reattaches_the_kb_and_lets_claude_create_the_new_session(self):
        sid = "11111111-2222-4333-8444-555555555555"
        from kb_claude_history import seed_new_session
        seed_new_session(self.root, kb_claude.build_context(self.loaded, "example", "latest.json"), session_id=sid)
        for fork in (["--resume", sid, "--fork-session"],):
            args = type("Args", (), {
                "name": "example", "file": "latest.json", "store": "test",
                "workspace": str(self.root), "claude_args": [*fork, "-p", "hi"],
            })()
            with mock.patch.object(kb_claude.os, "execvpe", side_effect=SystemExit) as execvp, \
                 mock.patch.object(kb_claude.os, "chdir"), \
                 contextlib.redirect_stdout(io.StringIO()) as stdout, \
                 self.assertRaises(SystemExit):
                kb_claude.start(args, {"stores": [self.loaded["store"]]})
            command = execvp.call_args.args[1]
            self.assertNotIn("--session-id", command)
            self.assertEqual(command[1:], [*fork, "-p", "hi"])
            self.assertIn("新しいセッションを作成します (--fork-session)", stdout.getvalue())
