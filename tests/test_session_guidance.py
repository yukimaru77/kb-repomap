import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_codex
from kb_fork_mint import CHARTER
from kb_items import guidance_item, load_items, load_session_items


class SessionGuidanceTests(unittest.TestCase):
    def test_launch_without_prompt_persists_guidance_and_diff_without_user_turn(self):
        blob = {"type": "compaction", "encrypted_content": "opaque"}
        for context in ("", "SOURCE DIFF"):
            for run_initial_turn in (None, True, False):
                with self.subTest(context=context, run_initial_turn=run_initial_turn), \
                     tempfile.TemporaryDirectory() as temporary:
                    snapshot = Path(temporary) / "kb.json"
                    snapshot.write_text(json.dumps([blob]))
                    with mock.patch.object(kb_codex, "CodexAppServer") as constructor:
                        server = constructor.return_value.__enter__.return_value
                        server.start.return_value = "new-session"
                        sid = kb_codex.start_session(snapshot, Path(temporary), "example", context,
                                                     run_initial_turn=run_initial_turn)
                    self.assertEqual(sid, "new-session")
                    injected = server.request.call_args_list[0].args[1]["items"]
                    self.assertEqual(injected[:2], [guidance_item(), blob])
                    self.assertEqual(len(injected), 3 if context else 2)
                    if context:
                        self.assertEqual(injected[-1], {"type": "message", "role": "developer",
                                         "content": [{"type": "input_text", "text": context}]})
                    self.assertFalse(any(item.get("role") == "user" for item in injected))
                    server.run_turn.assert_not_called()
                    server.request.assert_any_call("thread/name/set", {
                        "threadId": sid, "name": "example KBを活用する"})
                    constructor.return_value.__exit__.assert_called_once()

    def test_explicit_no_turn_overrides_prompt_without_losing_diff(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "kb.json"
            snapshot.write_text('[{"type":"compaction","encrypted_content":"opaque"}]')
            with mock.patch.object(kb_codex, "CodexAppServer") as constructor:
                server = constructor.return_value.__enter__.return_value
                server.start.return_value = "new-session"
                kb_codex.start_session(snapshot, Path(temporary), "example", "SOURCE DIFF",
                                       prompt="Run this later", run_initial_turn=False)
            server.run_turn.assert_not_called()
            injected = server.request.call_args_list[0].args[1]["items"]
            self.assertEqual(injected[-1]["content"][0]["text"], "SOURCE DIFF")

    def test_runtime_guidance_preserves_memories_user_notes_and_saved_bytes(self):
        blob = {"type": "compaction", "encrypted_content": "opaque", "future": {"keep": 1}}
        note = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Use the pinned source revision."}]}
        for content in [CHARTER, [{"type": "input_text", "text": CHARTER}]]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temporary:
                snapshot = Path(temporary) / "kb.json"
                items = [blob, {"type": "message", "role": "user", "content": content}, note]
                snapshot.write_text(json.dumps(items))
                original = snapshot.read_bytes()
                actual = load_session_items(snapshot)
                self.assertEqual(actual[0]["role"], "developer")
                text = actual[0]["content"][0]["text"]
                self.assertIn("prior knowledge provided by the user", text)
                self.assertIn("search them efficiently", text)
                self.assertNotIn("KB", text)
                self.assertNotIn("account pool", text)
                self.assertEqual(actual[1:], [blob, note])
                self.assertEqual(load_items(snapshot), items)
                self.assertEqual(snapshot.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
