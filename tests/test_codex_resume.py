import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import kb_codex_resume as resume
from kb_items import REPO_MAP_HEADER, developer_item


def message(role, text, kinds=None):
    item = {"type": "message", "role": role, "content": [{"type": "input_text", "text": text}]}
    if kinds:
        item["internal_chat_message_metadata_passthrough"] = {"content_item_kinds": kinds}
    return item


AGENTS = message("user", "# AGENTS.md instructions for /w\n...", ["agents_md.instructions"])
PERMISSIONS = message("developer", "<permissions instructions>sandboxed</permissions instructions>")
ENVIRONMENT = message("user", "<environment_context><cwd>/w</cwd></environment_context>")
SKILLS = message("developer", "<skills_instructions>...</skills_instructions>")
SUMMARY = message("user", resume.SUMMARY_PREFIX + " and produced a summary.\nsummary")
KB = [{"type": "compaction", "encrypted_content": "kb-blob-1"},
      {"type": "compaction", "encrypted_content": "kb-blob-2"},
      message("user", REPO_MAP_HEADER + "current map"),
      developer_item("dev.txt guidance")]


def user(text):
    return message("user", text, ["user.text"])


def assistant(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def items(*payloads):
    return [{"type": "response_item", "payload": payload} for payload in payloads]


def compacted(*replacement):
    return {"type": "compacted", "payload": {"message": "", "replacement_history": list(replacement),
                                             "window_number": 1}}


def meta(thread_id="original", **extra):
    return {"type": "session_meta", "payload": {"id": thread_id, "cwd": "/w", **extra}}


class EffectiveHistoryTests(unittest.TestCase):
    def converted(self, records, kb_thread=False, kb_items=KB):
        return resume.convert(resume.effective_history(records), kb_items, kb_thread=kb_thread)

    def test_no_compaction_inserts_after_leading_initial_context(self):
        records = [meta(), *items(AGENTS, PERMISSIONS, ENVIRONMENT, user("codeword PLUM"), assistant("OK"))]
        # Codex writes fresh initial context before the injected items.
        self.assertEqual(self.converted(records), [*KB, user("codeword PLUM"), assistant("OK")])

    def test_one_compaction_uses_replacement_history_and_later_items(self):
        records = [meta(), *items(AGENTS, ENVIRONMENT, user("old turn"), assistant("superseded")),
                   compacted(user("old turn"), SKILLS, AGENTS, user("latest"), SUMMARY),
                   {"type": "turn_context", "payload": {}},
                   *items(user("after"), assistant("reply"))]
        # The stale initial-context bundle is dropped; Codex writes a fresh one first.
        self.assertEqual(self.converted(records), [
            *KB, user("old turn"), user("latest"), SUMMARY, user("after"), assistant("reply")])
        self.assertNotIn(assistant("superseded"), self.converted(records))

    def test_only_the_replacement_bundle_is_dropped(self):
        switch = message("developer", "<collaboration_mode>plan</collaboration_mode>",
                         ["collaboration_mode.instructions"])
        records = [meta(), compacted(user("q"), PERMISSIONS, SKILLS, ENVIRONMENT, SUMMARY),
                   *items(switch, user("next"))]
        self.assertEqual(self.converted(records), [*KB, user("q"), SUMMARY, switch, user("next")])

    def test_last_of_several_compactions_wins(self):
        remote_summary = {"type": "compaction", "encrypted_content": "native-summary"}
        records = [meta(), *items(AGENTS, user("first")),
                   compacted(user("first"), SUMMARY), *items(user("second"), assistant("a2")),
                   compacted(user("first"), user("second"), remote_summary), *items(user("third"))]
        self.assertEqual(self.converted(records),
                         [*KB, user("first"), user("second"), remote_summary, user("third")])

    def test_previous_kb_items_are_replaced_not_duplicated(self):
        old_kb = [{"type": "compaction", "encrypted_content": "stale-blob"},
                  message("user", REPO_MAP_HEADER + "old map"), developer_item("old dev.txt"),
                  developer_item("old update context")]
        records = [meta(), *items(AGENTS, PERMISSIONS, *old_kb, user("question"))]
        self.assertEqual(self.converted(records, kb_thread=True), [*KB, user("question")])
        # Without kb provenance only exact copies of the current KB are recognized.
        records = [meta(), *items(AGENTS, *KB, user("question"))]
        self.assertEqual(self.converted(records), [*KB, user("question")])

    def test_compacted_kb_thread_drops_retained_kb_guidance(self):
        records = [meta(), *items(AGENTS, *KB, user("q")),
                   compacted(developer_item("dev.txt guidance"), user("q"), SUMMARY)]
        self.assertEqual(self.converted(records, kb_thread=True), [*KB, user("q"), SUMMARY])

    def test_foreign_leading_material_is_kept(self):
        foreign = {"type": "compaction", "encrypted_content": "someone else's blob"}
        records = [meta(), *items(AGENTS, developer_item("custom instructions"), foreign, user("q"))]
        self.assertEqual(self.converted(records), [developer_item("custom instructions"), *KB, foreign, user("q")])

    def test_rollback_drops_last_user_turns(self):
        records = [meta(), *items(AGENTS, user("one"), assistant("1"), user("two"), assistant("2")),
                   {"type": "event_msg", "payload": {"type": "thread_rolled_back", "num_turns": 1}},
                   *items(user("three"))]
        self.assertEqual(self.converted(records), [*KB, user("one"), assistant("1"), user("three")])

    def test_legacy_compaction_is_refused(self):
        records = [meta(), {"type": "compacted", "payload": {"message": "summary"}}]
        with self.assertRaises(ValueError):
            resume.effective_history(records)


class RolloutTests(unittest.TestCase):
    def write(self, path, records):
        path.write_text("".join(json.dumps(record) + "\n" for record in records))

    def test_follows_paginated_history_base(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent, child = root / "parent.jsonl", root / "child.jsonl"
            self.write(parent, [{**meta("parent"), "ordinal": 0},
                                *[{**record, "ordinal": index + 1} for index, record in
                                  enumerate(items(AGENTS, user("inherited"), user("after fork point")))]])
            self.write(child, [{**meta("child", history_base={"thread_id": "parent", "end_ordinal_exclusive": 3}),
                                "ordinal": 3}, {**items(user("child turn"))[0], "ordinal": 4}])
            records = resume.read_rollout(child, {"parent": parent}.__getitem__)
        self.assertEqual(resume.effective_history(records), [AGENTS, user("inherited"), user("child turn")])


def event(kind, **fields):
    return {"timestamp": "t", "type": "event_msg", "payload": {"type": kind, **fields}}


class DisplayTests(unittest.TestCase):
    def test_copies_transcript_events_and_applies_rollback_without_copying_it(self):
        records = [meta(), event("thread_settings_applied"), event("task_started", turn_id="1"),
                   event("item_completed", n=1), event("token_count"), event("task_complete"),
                   *items(user("model only")), compacted(user("x")),
                   event("task_started", turn_id="2"), event("item_completed", n=2),
                   event("thread_rolled_back", num_turns=1),
                   event("task_started", turn_id="3"), event("item_completed", n=3)]
        self.assertEqual([e["payload"] for e in resume.display_events(records)], [
            {"type": "task_started", "turn_id": "1"}, {"type": "item_completed", "n": 1},
            {"type": "task_complete"},
            {"type": "task_started", "turn_id": "3"}, {"type": "item_completed", "n": 3}])

    def test_append_continues_ordinals_and_leaves_model_history_alone(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rollout = root / "new.jsonl"
            rollout.write_text("".join(json.dumps({**record, "ordinal": index}) + "\n" for index, record in
                                       enumerate([meta("new"), *items(*KB, user("PLUM"))])))
            before = resume.effective_history(resume.read_rollout(rollout, None))
            events = resume.display_events([event("task_started", turn_id="1"), event("item_completed")])
            resume.append_events(rollout, "new", events, home=root)
            records = [json.loads(line) for line in rollout.read_text().splitlines()]
            self.assertEqual([record["ordinal"] for record in records], list(range(len(records))))
            self.assertEqual(records[-1]["payload"], {"type": "item_completed"})
            self.assertEqual(resume.effective_history(resume.read_rollout(rollout, None)), before)

    def test_append_refuses_a_thread_with_an_active_writer(self):
        import fcntl
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.object(resume, "LOCK_WAIT_SECONDS", 0):
            root = Path(temporary)
            rollout = root / "new.jsonl"
            rollout.write_text(json.dumps(meta("new")) + "\n")
            (root / "thread-writer-locks").mkdir()
            with (root / "thread-writer-locks" / "new.lock").open("a+") as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                with self.assertRaises(ValueError):
                    resume.append_events(rollout, "new", [{"timestamp": "t", "payload": {"type": "error"}}],
                                         home=root)
            self.assertEqual(rollout.read_text(), json.dumps(meta("new")) + "\n")


class FakeServer:
    def __init__(self, threads):
        self.threads = threads
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/read":
            if params["threadId"] == "converted":
                return {"thread": {"id": "converted", "path": "/new.jsonl"}}
            return {"thread": self.threads[params["threadId"]]}
        if method == "thread/list":
            return {"data": [self.threads["original"]]}
        if method == "thread/start":
            return {"thread": {"id": "converted"}}
        return {}


class ResumeSessionTests(unittest.TestCase):
    def test_creates_new_thread_and_leaves_original_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            rollout = Path(temporary) / "rollout.jsonl"
            rollout.write_text("".join(json.dumps(record) + "\n" for record in
                                       [meta(), *items(AGENTS, user("PLUM"), assistant("OK"))]))
            before = rollout.read_bytes()
            server = FakeServer({"original": {"id": "original", "path": str(rollout), "cwd": "/w",
                                              "model": "gpt-6-astra", "reasoningEffort": "high",
                                              "originator": "codex_exec", "name": None}})
            request = {"thread": None, "last": True, "all": False, "exec": True}
            original, converted, path, events = resume.resume_session(server, KB, "octane", request, Path("/w"))
            self.assertEqual((original, converted, path, events), ("original", "converted", "/new.jsonl", []))
            self.assertEqual(rollout.read_bytes(), before)
        methods = [method for method, _params in server.calls]
        self.assertEqual(methods, ["thread/list", "thread/read", "thread/start", "thread/inject_items",
                                   "thread/name/set", "thread/read"])
        start = server.calls[2][1]
        self.assertEqual((start["cwd"], start["model"], start["config"]),
                         ("/w", "gpt-6-astra", {"model_reasoning_effort": "high"}))
        self.assertEqual(server.calls[3][1], {"threadId": "converted",
                                              "items": [*KB, user("PLUM"), assistant("OK")]})
        self.assertEqual(server.calls[0][1]["cwd"], "/w")


if __name__ == "__main__":
    unittest.main()
