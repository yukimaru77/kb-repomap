import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.request
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import kb_cli
import kb_remote_proxy as proxy
import kb_resume
from test_kb_remote_proxy import DEV, ITEMS, USER, Upstream

SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"
OTHER = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0002"
BLOCK = {"type": "text", "text": "KB知識"}


def post(url, body, headers=None):
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


class Isolated(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.bindings = Path(directory.name)
        for patcher in (mock.patch.object(proxy, "BINDINGS", self.bindings),
                        mock.patch.dict("os.environ", {"KB_REMOTE_MODE": "provider"}),
                        mock.patch("sys.stderr", new_callable=io.StringIO)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.upstream = Upstream()
        self.addCleanup(self.upstream.close)


class ArgvTests(unittest.TestCase):
    def test_codex_resume_ids(self):
        self.assertEqual(kb_resume.codex_resume_id(["resume", SID]), (True, SID))
        self.assertEqual(kb_resume.codex_resume_id(["resume", "-m", "x", SID, "go on"]), (True, SID))
        self.assertEqual(kb_resume.codex_resume_id(["exec", "resume", "--last", "hi"]), (True, None))
        self.assertEqual(kb_resume.codex_resume_id(["resume"]), (True, None))
        self.assertEqual(kb_resume.codex_resume_id(["exec", "hi"]), (False, None))
        self.assertEqual(kb_resume.codex_resume_id(["fork", SID, "go on"]), (True, SID))
        self.assertEqual(kb_resume.codex_resume_id(["-m", "x", "fork", "--last", "--all"]), (True, None))
        self.assertEqual(kb_resume.codex_resume_id(["exec", "fork", SID, "hi"]), (True, SID))
        self.assertEqual(kb_resume.codex_resume_id(["exec", "--", "fork"]), (False, None))

    def test_claude_resume_ids(self):
        self.assertEqual(kb_resume.claude_resume_id(["--resume", SID, "-p", "x"]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id(["-r", SID]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id([f"--resume={SID}"]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id(["--resume"]), (True, None))
        self.assertEqual(kb_resume.claude_resume_id(["--continue", "-p", "x"]), (True, None))
        self.assertEqual(kb_resume.claude_resume_id(["-p", "x"]), (False, None))
        self.assertTrue(kb_resume.claude_fork(["--resume", SID, "--fork-session"]))
        self.assertFalse(kb_resume.claude_fork(["--continue", "--", "--fork-session"]))

    def test_claude_latest_session_is_the_newest_transcript_of_the_directory(self):
        import os
        with tempfile.TemporaryDirectory() as home:
            env = {"CLAUDE_CONFIG_DIR": home}
            workspace = Path("/private/tmp/a.b_c")
            self.assertIsNone(kb_resume.claude_latest_session(workspace, env))
            directory = Path(home) / "projects" / "-private-tmp-a-b-c"
            directory.mkdir(parents=True)
            for index, name in enumerate((SID, OTHER, "agent-x")):
                path = directory / f"{name}.jsonl"
                path.write_text("{}\n")
                os.utime(path, (1000 + index, 1000 + index))
            self.assertEqual(kb_resume.claude_latest_session(workspace, env), OTHER)

    def test_cli_resume_mode_needs_no_kb_name(self):
        with mock.patch.object(kb_cli.store, "read_config", return_value={"c": 1}), \
             mock.patch("kb_resume.main", return_value=4) as resume, self.assertRaises(SystemExit) as caught:
            kb_cli.main(["--remote", "codex", "resume", SID])
        resume.assert_called_once_with("codex", ["resume", SID], {"c": 1})
        self.assertEqual(caught.exception.code, 4)
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
             mock.patch("kb_resume.main", return_value=0) as resume, self.assertRaises(SystemExit):
            kb_cli.main(["--remote", "claude", "--resume", SID])
        resume.assert_called_once_with("claude", ["--resume", SID], {})

    def test_resume_without_resume_verb_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "専用"):
            kb_resume.resume_codex(["exec", "hi"], {})
        with self.assertRaisesRegex(ValueError, "専用"):
            kb_resume.resume_claude(["-p", "hi"], {})


class CodexResumeTests(Isolated):
    """Codex --remote resume is stealth-only: kb hands the addon a payload, Codex stays unchanged."""

    def run_codex(self, native_args):
        import kb_stealth
        seen = {}

        def runner(command, env, cwd):
            seen["plain"] = command
            return mock.Mock(returncode=0)

        def launch(client, command, payload, workspace, **kwargs):
            seen.update(client=client, command=command, payload=payload, kwargs=kwargs)
            return 0

        with mock.patch.object(kb_resume, "codex_items", return_value=ITEMS) as items, \
             mock.patch.object(kb_stealth, "launch", side_effect=launch), \
             mock.patch.dict("os.environ", {"KB_REMOTE_MODE": ""}):
            code = kb_resume.resume_codex(native_args, {}, runner=runner)
        return code, seen, items

    def addon_items(self, headers):
        """What the stealth addon's binder injects for a lazy (unbound) payload."""
        binder = kb_resume.CodexBinder({}, None, None)
        with mock.patch.object(kb_resume, "codex_items", return_value=ITEMS):
            binder.observe_payload(headers, {"input": [DEV, USER]})
        return binder.items(headers, {})

    def test_known_id_injects_current_kb(self):
        proxy.write_binding(SID, "codex", "octane", "pepabo", "latest.json")
        code, seen, items = self.run_codex(["resume", SID])
        self.assertEqual(code, 0)
        self.assertEqual(items.call_args.args[1]["name"], "octane")
        self.assertEqual((seen["client"], seen["command"]), ("codex", ["codex", "resume", SID]))
        self.assertEqual(seen["payload"]["items"], ITEMS)
        self.assertEqual(seen["payload"]["record"]["name"], "octane")
        self.assertNotIn("provider", seen["kwargs"])

    def test_unknown_id_launches_plain_codex(self):
        code, seen, _items = self.run_codex(["resume", SID])
        self.assertEqual(code, 0)
        self.assertEqual(seen["plain"], ["codex", "resume", SID])
        self.assertNotIn("payload", seen)
        self.assertIn("--remote セッションではない", sys.stderr.getvalue())

    def test_interactive_resume_resolves_binding_lazily(self):
        proxy.write_binding(SID, "codex", "octane", None, "latest.json")
        _code, seen, _items = self.run_codex(["resume"])
        self.assertEqual(seen["payload"], {"client": "codex", "record": None, "items": None})
        self.assertEqual(self.addon_items({"Thread-Id": SID}), ITEMS)

    def test_known_id_fork_binds_the_new_thread_to_the_source_kb(self):
        proxy.write_binding(SID, "codex", "octane", "pepabo", "v1.json")
        _code, seen, _items = self.run_codex(["fork", SID, "go on"])
        self.assertEqual(seen["command"], ["codex", "fork", SID, "go on"])
        self.assertEqual(seen["payload"]["items"], ITEMS)
        self.assertIn("フォーク", sys.stderr.getvalue())
        # What the addon's binder does with the fork's first request (a new thread id).
        binder = kb_resume.CodexBinder({}, seen["payload"]["record"], seen["payload"]["items"])
        binder.observe_payload({}, {"client_metadata": {"thread_id": OTHER, "session_id": OTHER}})
        self.assertEqual(binder.items({}, {}), ITEMS)
        record = proxy.read_binding(OTHER, "codex")
        self.assertEqual((record["name"], record["store"], record["file"]), ("octane", "pepabo", "v1.json"))

    def test_lazy_fork_resolves_the_source_from_forked_from_thread_id(self):
        proxy.write_binding(SID, "codex", "octane", "pepabo", "v1.json")
        _code, seen, _items = self.run_codex(["fork", "--last"])
        self.assertIsNone(seen["payload"]["record"])
        turn = json.dumps({"thread_id": OTHER, "session_id": OTHER, "forked_from_thread_id": SID})
        for headers, payload in (({"X-Codex-Turn-Metadata": turn}, {"input": [USER]}),
                                 ({}, {"client_metadata": {"thread_id": OTHER, "x-codex-turn-metadata": turn}})):
            with self.subTest(headers=headers):
                proxy.binding_path(OTHER).unlink(missing_ok=True)
                binder = kb_resume.CodexBinder({}, None, None)
                with mock.patch.object(kb_resume, "codex_items", return_value=ITEMS) as items:
                    binder.observe_payload(headers, payload)
                self.assertEqual(items.call_args.args[1]["name"], "octane")
                self.assertEqual(binder.items({}, {}), ITEMS)
                record = proxy.read_binding(OTHER, "codex")
                self.assertEqual((record["name"], record["file"]), ("octane", "v1.json"))
                self.assertEqual(proxy.read_binding(SID, "codex")["file"], "v1.json")

    def test_lazy_unknown_session_is_forwarded_unchanged(self):
        proxy.write_binding(SID, "codex", "octane", None, "latest.json")
        self.assertIsNone(self.addon_items({"Thread-Id": OTHER}))

    def test_provider_mode_is_rejected_for_codex(self):
        proxy.write_binding(SID, "codex", "octane", "pepabo", "latest.json")
        with mock.patch.object(kb_resume, "codex_items", return_value=ITEMS), \
                self.assertRaisesRegex(ValueError, "ステルス方式のみ"):
            kb_resume.resume_codex(["resume", SID], {}, runner=mock.Mock())


class CodexBindingTests(Isolated):
    def test_new_session_is_bound_from_first_request(self):
        binder = kb_resume.CodexBinder({}, {"name": "octane", "store": "pepabo", "file": "v1.json"}, ITEMS)
        body = json.dumps({"input": [], "client_metadata": {"thread_id": SID}}).encode()
        binder.observe("POST", "/responses", {"Session-Id": SID}, body)
        binder.observe("POST", "/models", {"Session-Id": OTHER}, b"")
        record = proxy.read_binding(SID, "codex")
        self.assertEqual((record["name"], record["store"], record["file"]), ("octane", "pepabo", "v1.json"))
        self.assertEqual(record["id_source"], "client_metadata.thread_id")
        self.assertIsNone(proxy.read_binding(OTHER))


PROMPT = {"role": "user", "content": "q"}


class ClaudeResumeTests(Isolated):
    def run_claude(self, claude_args, body):
        seen = {}

        def runner(command, env, cwd):
            seen.update(command=command, env=env)
            if env.get("ANTHROPIC_BASE_URL", "").startswith("http://127.0.0.1"):
                post(env["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true", body)
            return mock.Mock(returncode=0)

        with mock.patch.dict("os.environ", {"KB_CLAUDE_UPSTREAM": self.upstream.url}), \
             mock.patch.object(kb_resume, "claude_block", return_value=BLOCK):
            code = kb_resume.resume_claude(claude_args, {}, runner=runner)
        return code, seen

    def test_known_id_injects(self):
        proxy.write_binding(SID, "claude", "octane", None, "latest.json")
        _code, seen = self.run_claude(["--resume", SID, "-p", "hi"], {"system": "s", "messages": [PROMPT]})
        self.assertEqual(seen["command"], ["claude", "--resume", SID, "-p", "hi"])
        sent = json.loads(self.upstream.requests[0]["body"])
        self.assertEqual(sent["system"], "s")
        self.assertEqual(sent["messages"][0]["content"], [BLOCK, {"type": "text", "text": "q"}])

    def run_fork(self, claude_args, latest=None):
        user_id = json.dumps({"device_id": "d", "session_id": OTHER})
        with mock.patch.object(kb_resume, "claude_latest_session", return_value=latest):
            return self.run_claude(claude_args, {"system": "s", "messages": [PROMPT], "metadata": {"user_id": user_id}})

    def test_fork_of_known_id_injects_and_binds_the_new_session(self):
        proxy.write_binding(SID, "claude", "octane", "pepabo", "v1.json")
        _code, seen = self.run_fork(["--resume", SID, "--fork-session", "-p", "hi"])
        self.assertEqual(seen["command"], ["claude", "--resume", SID, "--fork-session", "-p", "hi"])
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["messages"][0]["content"][0], BLOCK)
        record = proxy.read_binding(OTHER, "claude")
        self.assertEqual((record["name"], record["store"], record["file"]), ("octane", "pepabo", "v1.json"))
        self.assertEqual(record["id_source"], "metadata.user_id.session_id")
        self.assertIn("フォーク", sys.stderr.getvalue())

    def test_continue_fork_uses_the_binding_of_the_session_it_continues(self):
        proxy.write_binding(SID, "claude", "octane", None, "latest.json")
        self.run_fork(["--continue", "--fork-session", "-p", "hi"], latest=SID)
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["messages"][0]["content"][0], BLOCK)
        self.assertEqual(proxy.read_binding(OTHER, "claude")["name"], "octane")

    def test_continue_fork_of_an_unbound_session_is_not_injected(self):
        self.run_fork(["-c", "--fork-session"], latest=SID)
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["messages"], [PROMPT])
        self.assertIsNone(proxy.read_binding(OTHER))

    def test_unknown_id_launches_plain_claude(self):
        with mock.patch.dict("os.environ", {"ANTHROPIC_BASE_URL": ""}):
            _code, seen = self.run_claude(["-r", SID], {"system": "s"})
        self.assertEqual(seen["command"], ["claude", "-r", SID])
        self.assertEqual(self.upstream.requests, [])
        self.assertIn("--remote セッションではない", sys.stderr.getvalue())

    def test_continue_resolves_binding_from_request_metadata(self):
        proxy.write_binding(SID, "claude", "octane", None, "latest.json")
        user_id = json.dumps({"device_id": "d", "session_id": SID})
        self.run_claude(["--continue", "-p", "hi"],
                        {"system": "s", "messages": [PROMPT], "metadata": {"user_id": user_id}})
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["messages"][0]["content"][0], BLOCK)

    def test_continue_unknown_session_is_not_injected(self):
        self.run_claude(["-c"], {"system": "s", "messages": [PROMPT],
                                 "metadata": {"user_id": f"user_x_account_y_session_{OTHER}"}})
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["messages"], [PROMPT])


class CodexItemsTests(unittest.TestCase):
    def test_all_items_are_injected_even_for_old_local_guidance_bindings(self):
        blob = {"type": "compaction", "encrypted_content": "opaque"}
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "kb.json"
            snapshot.write_text(json.dumps([blob]))
            dev = {"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": "notes\n"}]}
            loaded = {"jsonl": snapshot, "developer_text": "notes\n", "info": {"source_kind": "paper"}}
            with mock.patch.object(kb_resume, "_loaded", return_value=loaded):
                for record in ({"name": "kb", "file": "latest.json"},
                               {"name": "kb", "file": "latest.json", "local_guidance": True}):
                    with self.subTest(record=record):
                        self.assertEqual(kb_resume.codex_items({}, record), [blob, dev])


if __name__ == "__main__":
    unittest.main()
