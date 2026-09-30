import io
import json
from pathlib import Path
import re
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

    def test_claude_resume_ids(self):
        self.assertEqual(kb_resume.claude_resume_id(["--resume", SID, "-p", "x"]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id(["-r", SID]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id([f"--resume={SID}"]), (True, SID))
        self.assertEqual(kb_resume.claude_resume_id(["--resume"]), (True, None))
        self.assertEqual(kb_resume.claude_resume_id(["--continue", "-p", "x"]), (True, None))
        self.assertEqual(kb_resume.claude_resume_id(["-p", "x"]), (False, None))

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
        with self.assertRaisesRegex(ValueError, "再開専用"):
            kb_resume.resume_codex(["exec", "hi"], {})
        with self.assertRaisesRegex(ValueError, "再開専用"):
            kb_resume.resume_claude(["-p", "hi"], {})


class CodexResumeTests(Isolated):
    def run_codex(self, native_args, headers):
        seen = {}

        def runner(command, env, cwd):
            seen["command"] = command
            match = re.search(r'base_url = "(http://127\.0\.0\.1:\d+)"', " ".join(command))
            if match:
                post(match.group(1) + "/responses", {"input": [DEV, USER]}, headers)
            return mock.Mock(returncode=0)

        with mock.patch("kb_remote.pool_endpoint", return_value=(self.upstream.url.removesuffix("/backend-api/codex"), "POOLKEY")), \
             mock.patch.object(kb_resume, "codex_items", return_value=ITEMS) as items, \
             mock.patch("kb_native.proxy_overrides", side_effect=lambda url, provider=None: [
                 'model_provider="kb_pool"', f'model_providers.kb_pool={{ base_url = "{url}" }}']), \
             mock.patch.dict("os.environ", {"KB_CODEX_CONFIG_OVERRIDES": "[]"}):
            code = kb_resume.resume_codex(native_args, {}, runner=runner)
        return code, seen, items

    def test_known_id_injects_current_kb(self):
        proxy.write_binding(SID, "codex", "octane", "pepabo", "latest.json")
        code, seen, items = self.run_codex(["resume", SID], {"Session-Id": SID})
        self.assertEqual(code, 0)
        self.assertEqual(items.call_args.args[1]["name"], "octane")
        self.assertEqual(seen["command"][:2], ["codex", "resume"])
        self.assertEqual(seen["command"][-1], SID)
        body = json.loads(self.upstream.requests[0]["body"])
        self.assertEqual(body["input"], [DEV, *ITEMS, USER])
        self.assertEqual(self.upstream.requests[0]["headers"]["Authorization"], "Bearer POOLKEY")

    def test_unknown_id_launches_plain_codex(self):
        code, seen, _items = self.run_codex(["resume", SID], {"Session-Id": SID})
        self.assertEqual(code, 0)
        self.assertNotIn("base_url", " ".join(seen["command"]))
        self.assertEqual(self.upstream.requests, [])
        self.assertIn("--remote セッションではない", sys.stderr.getvalue())

    def test_interactive_resume_resolves_binding_lazily(self):
        proxy.write_binding(SID, "codex", "octane", None, "latest.json")
        self.run_codex(["resume"], {"Thread-Id": SID})
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["input"], [DEV, *ITEMS, USER])

    def test_lazy_unknown_session_is_forwarded_unchanged(self):
        self.run_codex(["resume", "--last"], {"Thread-Id": OTHER})
        self.assertEqual(json.loads(self.upstream.requests[0]["body"])["input"], [DEV, USER])


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
