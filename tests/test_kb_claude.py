import io
import json
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
        with mock.patch.object(kb_claude.os, "execvp", side_effect=SystemExit) as execvp, \
             self.assertRaises(SystemExit):
            kb_claude.start(args, {"stores": [self.loaded["store"]]})
        command = execvp.call_args.args[1]
        self.assertEqual(command[0:2], ["claude", "--session-id"])
        self.assertIn("--append-system-prompt", command)
        self.assertIn("復号された知識", " ".join(command))
        self.assertIn("--model", command)
