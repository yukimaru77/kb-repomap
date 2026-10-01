"""Pure rules for stealth mode: WebSocket/HTTP conversation tracking."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_remote_proxy as proxy


KB = [{"type": "compaction", "encrypted_content": "KB-one"},
      {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "KB-two"}]}]


def create(text):
    return json.loads(text)


class ConversationTests(unittest.TestCase):
    """Cases from TestRemoteKBWebSocketDeltaCompactionAndReinjection."""

    def test_delta_compaction_and_reinjection(self):
        s = proxy.CodexConversation()
        full = create('{"type":"response.create","generate":false,"input":[{"role":"developer","content":"rules"},'
                      '{"role":"user","content":"question"}],"unknown":1e+00}')
        out = s.transform(full, KB)
        self.assertIsNotNone(out)
        self.assertEqual([item.get("encrypted_content") for item in out["input"]][1], "KB-one")
        self.assertEqual(out["input"][0]["role"], "developer")
        self.assertEqual(out["input"][-1]["content"], "question")
        s.event({"type": "response.completed", "response": {"id": "prewarm", "output": []}})

        # A normal delta keeps previous_response_id and reuses the upstream KB prefix.
        delta = create('{"type":"response.create","previous_response_id":"prewarm",'
                       '"input":[{"role":"user","content":"second"}]}')
        self.assertIsNone(s.transform(delta, KB))
        s.event({"type": "response.output_item.done", "item": {"role": "assistant", "content": "answer"}})
        s.event({"type": "response.completed", "response": {"id": "inference", "output": []}})

        # Compaction must not see the KB: expand to the client-visible history.
        compact = create('{"type":"response.create","previous_response_id":"inference",'
                         '"input":[{"type":"compaction_trigger"}],"unknown":1e+00}')
        out = s.transform(compact, KB, compaction=True)
        text = json.dumps(out)
        self.assertNotIn("KB-one", text)
        self.assertIsNone(out["previous_response_id"])
        self.assertEqual(len(out["input"]), 5)
        self.assertEqual(out["unknown"], 1)
        s.event({"type": "response.completed", "response": {"id": "compact", "output": [
            {"type": "compaction_summary", "encrypted_content": "conversation-blob"}]}})

        # Switching back to inference on a reused connection restores the KB.
        out = s.transform(create('{"type":"response.create","previous_response_id":"compact",'
                                 '"input":[{"role":"user","content":"next"}]}'), KB)
        text = json.dumps(out)
        self.assertIn("KB-one", text)
        self.assertNotIn("compaction_trigger", text)
        self.assertIsNone(out["previous_response_id"])

        # Native post-compact requests send a new full context; KB goes first.
        out = s.transform(create('{"type":"response.create","input":[{"role":"developer"},'
                                 '{"type":"compaction_summary","encrypted_content":"new-conversation"},'
                                 '{"role":"user"}]}'), KB)
        text = json.dumps(out)
        self.assertLess(text.index("KB-two"), text.index("new-conversation"))

    def test_unknown_previous_response_passes_through_untracked(self):
        # The provider proxy rejects this; stealth mode must never break the client, so it
        # forwards unchanged and stops expanding until a full input arrives.
        s = proxy.CodexConversation()
        self.assertIsNone(s.transform(create('{"type":"response.create","previous_response_id":"unseen",'
                                             '"input":[]}'), KB))
        s.event({"type": "response.completed", "response": {"id": "r1", "output": []}})
        self.assertIsNone(s.transform(create('{"type":"response.create","previous_response_id":"r1",'
                                             '"input":[]}'), KB))
        out = s.transform(create('{"type":"response.create","input":[{"role":"user","content":"x"}]}'), KB)
        self.assertIn("KB-one", json.dumps(out))

    def test_failed_response_does_not_advance_history(self):
        s = proxy.CodexConversation()
        s.transform(create('{"type":"response.create","input":[]}'), KB)
        s.event({"type": "response.completed", "response": {"id": "r0", "output": []}})
        s.transform(create('{"type":"response.create","previous_response_id":"r0","input":[]}'), KB)
        s.event({"type": "response.failed"})
        self.assertEqual(s.last_id, "r0")

    def test_without_items_nothing_changes(self):
        s = proxy.CodexConversation()
        self.assertIsNone(s.transform(create('{"type":"response.create","input":[{"role":"user"}]}'), None))
        self.assertIsNone(s.transform({"type": "response.create", "input": "text"}, KB))

    def test_output_from_completed_event_when_no_items_streamed(self):
        s = proxy.CodexConversation()
        s.transform(create('{"type":"response.create","input":[{"role":"user","content":"q"}]}'), KB)
        s.event({"type": "response.completed", "response": {"id": "r0", "output": [{"role": "assistant"}]}})
        out = s.transform(create('{"type":"response.create","previous_response_id":"r0",'
                                 '"input":[{"type":"compaction_trigger"}]}'), KB, compaction=True)
        self.assertEqual(out["input"], [{"role": "user", "content": "q"}, {"role": "assistant"},
                                        {"type": "compaction_trigger"}])


class SSETests(unittest.TestCase):
    def test_events_split_across_chunks(self):
        seen = []
        parser = proxy.SSEEvents(seen.append)
        stream = (b'event: response.output_item.done\ndata: {"type":"response.output_item.done",'
                  b'"item":{"a":1}}\n\ndata: {"type":"response.completed","response":{"id":"r"}}\r\n\r\n'
                  b"data: [DONE]\n\n")
        for index in range(0, len(stream), 7):
            parser.feed(stream[index:index + 7])
        self.assertEqual([event["type"] for event in seen], ["response.output_item.done", "response.completed"])


if __name__ == "__main__":
    unittest.main()
