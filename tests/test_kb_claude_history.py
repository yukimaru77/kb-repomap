import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_claude_history
import kb_claude_remote


class ClaudeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.env = {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}

    def test_seed_is_private_and_uses_exact_remote_user_block(self):
        sid, path = kb_claude_history.seed_new_session(self.workspace, "synthetic KB", env=self.env)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        item = json.loads(path.read_text())
        self.assertEqual(item["sessionId"], sid)
        self.assertEqual(item["message"], {"role": "user", "content": kb_claude_remote.kb_block("synthetic KB")["text"]})
        self.assertIsNone(item["parentUuid"])

    def test_never_overwrites_existing_session(self):
        sid, path = kb_claude_history.seed_new_session(self.workspace, "first", env=self.env)
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            kb_claude_history.seed_new_session(self.workspace, "second", session_id=sid, env=self.env)
        self.assertEqual(path.read_bytes(), before)

    def test_bad_session_id_is_rejected(self):
        with self.assertRaises(ValueError):
            kb_claude_history.seed_new_session(self.workspace, "x", session_id="../../escape", env=self.env)

    def test_partial_file_removed_on_write_failure(self):
        with mock.patch("kb_claude_history.json.dump", side_effect=OSError("write failed")):
            with self.assertRaises(OSError):
                kb_claude_history.seed_new_session(self.workspace, "x", env=self.env)
        self.assertEqual(list((self.root / "claude").rglob("*.jsonl")), [])

    def test_resume_requires_same_uncompacted_seed(self):
        sid, path = kb_claude_history.seed_new_session(self.workspace, "original", env=self.env)
        kb_claude_history.require_existing_seed(self.workspace, sid, "original", env=self.env)
        with self.assertRaisesRegex(ValueError, "changed"):
            kb_claude_history.require_existing_seed(self.workspace, sid, "changed", env=self.env)
        with path.open("a") as output:
            output.write(json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n")
        with self.assertRaisesRegex(ValueError, "compacted"):
            kb_claude_history.require_existing_seed(self.workspace, sid, "original", env=self.env)

    def test_seed_on_abandoned_branch_is_not_accepted(self):
        sid, path = kb_claude_history.seed_new_session(self.workspace, "original", env=self.env)
        with path.open("a") as output:
            output.write(json.dumps({"type": "user", "uuid": "other-leaf", "parentUuid": None,
                                     "message": {"role": "user", "content": "other branch"}}) + "\n")
        with self.assertRaisesRegex(ValueError, "seed missing"):
            kb_claude_history.require_existing_seed(self.workspace, sid, "original", env=self.env)

    def test_resume_traverses_attachment_parent_chain(self):
        sid, path = kb_claude_history.seed_new_session(self.workspace, "original", env=self.env)
        seed = json.loads(path.read_text())
        with path.open("a") as output:
            for item in [
                {"type": "attachment", "uuid": "attachment", "parentUuid": seed["uuid"]},
                {"type": "assistant", "uuid": "answer", "parentUuid": "attachment"},
            ]:
                output.write(json.dumps(item) + "\n")
        kb_claude_history.require_existing_seed(self.workspace, sid, "original", env=self.env)


if __name__ == "__main__":
    unittest.main()