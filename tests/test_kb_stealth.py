"""kb_stealth launcher: mode selection, child env, and mitmdump lifetime (fake mitmdump)."""
import io
import json
import os
from pathlib import Path
import signal
import socket
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_remote_proxy as proxy
import kb_stealth

FAKE = """#!{python}
import json, os, signal, socket, sys
argv = sys.argv[1:]
port = int(argv[argv.index("-p") + 1])
record = os.environ["FAKE_MITM_RECORD"]
sets = [argv[i + 1] for i, v in enumerate(argv) if v == "--set"]
payload = next(v.split("=", 1)[1] for v in sets if v.startswith("kb_payload="))
with open(record, "w") as handle:
    json.dump({{"argv": argv, "pid": os.getpid(), "payload": json.load(open(payload)),
               "mode": oct(os.stat(payload).st_mode & 0o777)}}, handle)
if os.environ.get("FAKE_MITM_FAIL"):
    sys.exit(3)
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
def stop(*_args):
    open(record + ".terminated", "w").close()
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    server.accept()[0].close()
"""


class FakeMitmdump(unittest.TestCase):
    """A fake mitmdump on PATH that records its argv and payload, and listens on -p."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        self.fake = bin_dir / "mitmdump"
        self.fake.write_text(FAKE.format(python=sys.executable))
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.record = self.dir / "record.json"
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_MITM_RECORD": str(self.record),
                    "KB_CA_DIR": str(self.dir / "ca"), "HOME": os.environ.get("HOME", "")}
        for patcher in (mock.patch.object(proxy, "BINDINGS", self.dir / "bindings"),
                        mock.patch.object(kb_stealth, "FALLBACK_MITMDUMP", self.dir / "absent"),
                        mock.patch("sys.stderr", new_callable=io.StringIO)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def launch(self, client, env=None, code=5):
        seen = {"provider": False}

        def runner(command, env, cwd):
            port = int(env["HTTPS_PROXY"].rsplit(":", 1)[1])
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
            seen.update(command=command, env=env, cwd=cwd)
            return mock.Mock(returncode=code)

        def provider():
            seen["provider"] = True
            return 9

        result = kb_stealth.launch(client, [client, "-p", "hi"], {"client": client, "record": None}, self.dir,
                                   provider=provider, describe=lambda port: f"Remote KB: proxy 127.0.0.1:{port}",
                                   runner=runner, env=self.env if env is None else env)
        return result, seen

    def wait_terminated(self):
        for _ in range(100):
            if Path(str(self.record) + ".terminated").exists():
                return True
            time.sleep(0.05)
        return False

class LauncherTests(FakeMitmdump):
    def test_codex_child_env_and_lifetime(self):
        code, seen = self.launch("codex")
        self.assertEqual(code, 5)
        self.assertFalse(seen["provider"])
        record = json.loads(self.record.read_text())
        argv = record["argv"]
        port = argv[argv.index("-p") + 1]
        self.assertEqual(seen["env"]["HTTPS_PROXY"], f"http://127.0.0.1:{port}")
        self.assertNotIn("NODE_EXTRA_CA_CERTS", seen["env"])
        for name in ("ANTHROPIC_BASE_URL", "ENABLE_TOOL_SEARCH", "KB_NATIVE_POOL_KEY"):
            self.assertNotIn(name, seen["env"])
        self.assertEqual(seen["command"], ["codex", "-p", "hi"])
        self.assertNotIn("model_provider", " ".join(seen["command"]))
        self.assertEqual(argv[:5], ["-q", "--listen-host", "127.0.0.1", "-p", port])
        self.assertIn(kb_stealth.ALLOW_HOSTS, argv)
        self.assertIn(f"confdir={self.dir / 'ca'}", argv)
        self.assertIn(str(kb_stealth.ADDON), argv)
        self.assertIn(f"kb_repo={kb_stealth.REPO}", argv)
        self.assertEqual(record["payload"], {"client": "codex", "record": None})
        self.assertEqual(record["mode"], "0o600")
        self.assertTrue(self.wait_terminated())
        payload = next(v.split("=", 1)[1] for v in argv if v.startswith("kb_payload="))
        self.assertFalse(Path(payload).exists())
        self.assertIn(f"Remote KB: proxy 127.0.0.1:{port} / stealth", sys.stderr.getvalue())

    def test_claude_gets_ca_bundle_and_keeps_base_url_untouched(self):
        env = dict(self.env, NO_PROXY="localhost")
        _code, seen = self.launch("claude", env)
        self.assertEqual(seen["env"]["NODE_EXTRA_CA_CERTS"], str(self.dir / "ca" / kb_stealth.CA_FILE))
        self.assertNotIn("ANTHROPIC_BASE_URL", seen["env"])
        self.assertNotIn("ENABLE_TOOL_SEARCH", seen["env"])
        self.assertEqual(seen["env"]["NO_PROXY"], "localhost")

    def test_mitmdump_stopped_when_child_fails(self):
        def runner(*_args, **_kwargs):
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            kb_stealth.launch("codex", ["codex"], {}, self.dir, provider=lambda: 0, describe=str,
                              runner=runner, env=self.env)
        self.assertTrue(self.wait_terminated())

    def test_mitmdump_stopped_when_kb_is_terminated(self):
        def runner(*_args, **_kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(5)

        previous = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(SystemExit) as caught:
            kb_stealth.launch("codex", ["codex"], {}, self.dir, provider=lambda: 0, describe=str,
                              runner=runner, env=self.env)
        self.assertEqual(caught.exception.code, 128 + signal.SIGTERM)
        self.assertTrue(self.wait_terminated())
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        self.assertIn(f"kb_parent={os.getpid()}", json.loads(self.record.read_text())["argv"])

    def test_fallback_when_mitmdump_absent(self):
        env = dict(self.env, PATH="/usr/bin:/bin")
        code, seen = self.launch("codex", env)
        self.assertEqual(code, 9)
        self.assertTrue(seen["provider"])
        self.assertIn(kb_stealth.NOT_FOUND, sys.stderr.getvalue())

    def test_explicit_stealth_without_mitmdump_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "mitmdump"):
            self.launch("codex", dict(self.env, PATH="/usr/bin:/bin", KB_REMOTE_MODE="stealth"))

    def test_mode_override_and_pool_rr(self):
        code, seen = self.launch("claude", dict(self.env, KB_REMOTE_MODE="provider"))
        self.assertEqual((code, seen["provider"]), (9, True))
        self.assertFalse(self.record.exists())
        rr = json.dumps(['model_provider="pool_rr"'])
        code, seen = self.launch("codex", dict(self.env, KB_CODEX_CONFIG_OVERRIDES=rr))
        self.assertTrue(seen["provider"])
        # pool-rr only concerns Codex; an empty override list is not pool-rr.
        _code, seen = self.launch("codex", dict(self.env, KB_CODEX_CONFIG_OVERRIDES="[]"))
        self.assertFalse(seen["provider"])
        with self.assertRaisesRegex(ValueError, "KB_REMOTE_MODE"):
            kb_stealth.select("codex", dict(self.env, KB_REMOTE_MODE="bogus"))

    def test_start_failure_falls_back_by_default_and_errors_when_explicit(self):
        env = dict(self.env, FAKE_MITM_FAIL="1")
        code, seen = self.launch("codex", env)
        self.assertTrue(seen["provider"])
        self.assertIn("stealth proxy failed", sys.stderr.getvalue())
        with self.assertRaisesRegex(ValueError, "stealth"):
            self.launch("codex", dict(env, KB_REMOTE_MODE="stealth"))

    def test_discovery_order(self):
        other = self.dir / "other-mitmdump"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        self.assertEqual(kb_stealth.find_mitmdump(dict(self.env, KB_MITMDUMP=str(other))), str(other))
        self.assertEqual(kb_stealth.find_mitmdump(self.env), str(self.fake))
        with mock.patch.object(kb_stealth, "FALLBACK_MITMDUMP", other):
            self.assertEqual(kb_stealth.find_mitmdump({"PATH": "/usr/bin:/bin"}), str(other))

    def test_tls_failure_hint_after_child_exit(self):
        def runner(command, env, cwd):
            proxy.log("stealth: client TLS failed for chatgpt.com: unknown ca; run: kb ca-setup")
            return mock.Mock(returncode=1)

        kb_stealth.launch("codex", ["codex"], {}, self.dir, provider=lambda: 0, describe=str,
                          runner=runner, env=self.env)
        self.assertIn("kb ca-setup", sys.stderr.getvalue())

    def test_ca_setup_output(self):
        (self.dir / "ca").mkdir()
        (self.dir / "ca" / kb_stealth.CA_FILE).write_text("pem")
        out = io.StringIO()
        runner = mock.Mock(return_value=mock.Mock(returncode=0))
        kb_stealth.ca_setup(self.env, out, runner=runner, platform="darwin")
        text = out.getvalue()
        self.assertIn(str(self.fake), text)
        self.assertIn("security add-trusted-cert -d -r trustRoot -k", text)
        self.assertIn(str(self.dir / "ca" / kb_stealth.CA_FILE), text)
        self.assertIn("登録済み", text)
        out = io.StringIO()
        kb_stealth.ca_setup(dict(self.env, PATH="/usr/bin:/bin"), out, runner=runner, platform="linux")
        self.assertIn("update-ca-certificates", out.getvalue())
        self.assertIn("見つかりません", out.getvalue())


class WiringTests(FakeMitmdump):
    """Entry points choose stealth by default and hand the addon the right material."""

    SID = "0199aaaa-bbbb-7ccc-8ddd-eeeeffff0001"

    def setUp(self):
        super().setUp()
        environment = {key: value for key, value in os.environ.items()
                       if key not in ("KB_REMOTE_MODE", "KB_CODEX_CONFIG_OVERRIDES", "ANTHROPIC_BASE_URL")}
        environment.update(self.env, KB_MITMDUMP=str(self.fake))
        patcher = mock.patch.dict(os.environ, environment, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.seen = {}

    def runner(self, command, env, cwd):
        self.seen.update(command=command, env=env, payload=json.loads(self.record.read_text())["payload"])
        return mock.Mock(returncode=4)

    def test_codex_resume_known_id(self):
        import kb_resume
        proxy.write_binding(self.SID, "codex", "octane", "pepabo", "latest.json")
        items = [{"type": "compaction", "encrypted_content": "KB"}]
        with mock.patch.object(kb_resume, "codex_items", return_value=items), \
             mock.patch("kb_remote.pool_endpoint") as endpoint:
            code = kb_resume.resume_codex(["resume", self.SID], {}, runner=self.runner)
        self.assertEqual(code, 4)
        endpoint.assert_not_called()
        self.assertEqual(self.seen["command"], ["codex", "resume", self.SID])
        self.assertEqual(self.seen["payload"]["items"], items)
        self.assertEqual(self.seen["payload"]["record"]["name"], "octane")
        self.assertIn("HTTPS_PROXY", self.seen["env"])
        self.assertIn("/ 1 items / stealth", sys.stderr.getvalue())

    def test_codex_resume_lazy(self):
        import kb_resume
        kb_resume.resume_codex(["resume", "--last"], {}, runner=self.runner)
        self.assertEqual(self.seen["payload"], {"client": "codex", "record": None, "items": None})
        self.assertIn("lazy items / stealth", sys.stderr.getvalue())

    def test_native_new_session(self):
        import kb_native
        args = kb_native.parse(["example", "--remote", "codex", "exec", "hi"])
        remote = mock.Mock(items=[{"type": "compaction"}])
        with mock.patch.object(kb_native, "RemoteKB", return_value=remote), \
             mock.patch.object(kb_native.proxy, "run_client",
                               side_effect=lambda c, e, w, r=None: self.runner(c, e, w).returncode):
            code = kb_native.run(args, {}, Path("kb.json"), self.dir, "")
        self.assertEqual(code, 4)
        self.assertEqual(self.seen["command"], ["codex", "exec", "hi"])
        self.assertEqual(self.seen["payload"]["record"]["name"], "example")
        self.assertEqual(self.seen["payload"]["items"], [{"type": "compaction"}])

    def test_claude_resume_known_id(self):
        import kb_resume
        proxy.write_binding(self.SID, "claude", "octane", None, "latest.json")
        block = {"type": "text", "text": "KB"}
        with mock.patch.object(kb_resume, "claude_block", return_value=block):
            code = kb_resume.resume_claude(["--resume", self.SID], {}, runner=self.runner)
        self.assertEqual(code, 4)
        self.assertEqual(self.seen["command"], ["claude", "--resume", self.SID])
        self.assertEqual(self.seen["payload"]["block"], block)
        self.assertNotIn("ANTHROPIC_BASE_URL", self.seen["env"])
        self.assertNotIn("ENABLE_TOOL_SEARCH", self.seen["env"])
        self.assertIn("NODE_EXTRA_CA_CERTS", self.seen["env"])
        self.assertIn("bytes / stealth", sys.stderr.getvalue())

    def test_claude_start_remote(self):
        import kb_claude_remote
        loaded = {"store": {"name": "s"}}
        args = type("Args", (), {"name": "example", "file": "latest.json", "store": None,
                                 "workspace": str(self.dir), "claude_args": ["-p", "hi"]})()
        with mock.patch.object(kb_claude_remote.store, "find_kb", return_value=loaded), \
             mock.patch.object(kb_claude_remote.kb_claude, "build_context", return_value="CTX"):
            code = kb_claude_remote.start_remote(args, {}, runner=self.runner)
        self.assertEqual(code, 4)
        self.assertEqual(self.seen["command"][:2], ["claude", "--session-id"])
        self.assertTrue(self.seen["payload"]["block"]["text"].endswith("CTX"))
        self.assertEqual(proxy.read_binding(self.seen["command"][2], "claude")["name"], "example")


if __name__ == "__main__":
    unittest.main()
