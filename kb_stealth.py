"""Stealth remote KB: run one client behind a per-session mitmdump.

The client keeps its own settings, provider, base URL and login; only
HTTPS_PROXY (and, for Claude Code, NODE_EXTRA_CA_CERTS) is added to its
environment. kb_stealth_addon.py edits the model requests inside mitmdump.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import kb_remote_proxy as proxy


ADDON = Path(__file__).resolve().with_name("kb_stealth_addon.py")
REPO = ADDON.parent
FALLBACK_MITMDUMP = Path.home() / "projects/codex-account-pool/bridge/.venv/bin/mitmdump"
ALLOW_HOSTS = r"^(chatgpt\.com|api\.anthropic\.com):443$"
CA_FILE = "mitmproxy-ca-cert.pem"
READY_TIMEOUT = 10.0
MODES = ("provider", "stealth")
NOT_FOUND = "Remote KB: provider mode (mitmdump not found; install: uv tool install mitmproxy)"
TLS_HINT = "Remote KB: クライアントがkbのCAを信頼していないため通信に失敗しました。kb ca-setup を実行してください"


class StealthError(RuntimeError):
    """mitmdump could not be started for this session."""


def notice(message):
    print(message, file=sys.stderr, flush=True)


def find_mitmdump(env=None):
    env = os.environ if env is None else env
    configured = env.get("KB_MITMDUMP")
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    found = shutil.which("mitmdump", path=env.get("PATH"))
    if found:
        return found
    return str(FALLBACK_MITMDUMP) if os.access(FALLBACK_MITMDUMP, os.X_OK) else None


def ca_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("KB_CA_DIR") or Path.home() / ".cache/kb/ca").expanduser()


def pool_rr(env=None):
    """pool-rr passes its round-robin provider through KB_CODEX_CONFIG_OVERRIDES."""
    env = os.environ if env is None else env
    try:
        return bool(json.loads(env.get("KB_CODEX_CONFIG_OVERRIDES") or "[]"))
    except ValueError:
        return True


@dataclass
class Selection:
    mode: str
    mitmdump: str = None
    explicit: bool = False


def select(client, env=None):
    """Pick stealth or provider mode for one --remote session."""
    env = os.environ if env is None else env
    wanted = (env.get("KB_REMOTE_MODE") or "").strip().lower()
    if wanted and wanted not in MODES:
        raise ValueError(f"KB_REMOTE_MODE は provider か stealth です: {wanted}")
    if client == "codex" and pool_rr(env):
        if wanted == "stealth":
            notice("Remote KB: pool-rr のため provider 方式で起動します（KB_REMOTE_MODE=stealth は無視）")
        return Selection("provider")
    if wanted == "provider":
        return Selection("provider", explicit=True)
    mitmdump = find_mitmdump(env)
    if mitmdump is None:
        if wanted == "stealth":
            raise ValueError("KB_REMOTE_MODE=stealth ですが mitmdump が見つかりません"
                             "（KB_MITMDUMP を指定するか uv tool install mitmproxy）")
        notice(NOT_FOUND)
        return Selection("provider")
    return Selection("stealth", mitmdump, explicit=wanted == "stealth")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _log_size():
    try:
        return (proxy.BINDINGS / "proxy.log").stat().st_size
    except OSError:
        return 0


class Session:
    """A running mitmdump for one client; use as a context manager."""

    def __init__(self, mitmdump, payload, env=None):
        self.env_base = dict(os.environ if env is None else env)
        self.ca = ca_dir(self.env_base)
        self.port = _free_port()
        self.process = None
        self.log_offset = _log_size()
        handle, name = tempfile.mkstemp(prefix="kb-stealth-", suffix=".json")
        self.payload = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as stream:  # mkstemp creates it 0600
            json.dump(payload, stream, ensure_ascii=False)
        try:
            self._start(mitmdump)
        except BaseException:
            self.close()
            raise

    def command(self, mitmdump):
        return [mitmdump, "-q", "--listen-host", "127.0.0.1", "-p", str(self.port),
                "--set", f"confdir={self.ca}", "--allow-hosts", ALLOW_HOSTS,
                "-s", str(ADDON), "--set", f"kb_payload={self.payload}",
                "--set", f"kb_bindings={proxy.BINDINGS}", "--set", f"kb_repo={REPO}"]

    def _start(self, mitmdump):
        proxy.BINDINGS.mkdir(parents=True, exist_ok=True)
        self.ca.mkdir(parents=True, exist_ok=True)
        # mitmdump's own output must never reach the client's terminal.
        output = (proxy.BINDINGS / "mitmdump.log").open("ab")
        try:
            self.process = subprocess.Popen(self.command(mitmdump), stdin=subprocess.DEVNULL, stdout=output,
                                            stderr=output, env=self.env_base, start_new_session=True)
        except OSError as error:
            raise StealthError(f"mitmdump を起動できません: {error}") from error
        finally:
            output.close()
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise StealthError(f"mitmdump が終了しました (exit {self.process.returncode}); "
                                   f"詳細: {proxy.BINDINGS / 'mitmdump.log'}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.5):
                    return
            except OSError:
                time.sleep(0.05)
        raise StealthError(f"mitmdump が {READY_TIMEOUT:.0f} 秒以内に 127.0.0.1:{self.port} で待ち受けませんでした")

    def client_env(self, client):
        env = dict(self.env_base)
        url = f"http://127.0.0.1:{self.port}"
        env["HTTPS_PROXY"] = url
        if "https_proxy" in env:
            env["https_proxy"] = url
        if client == "claude":
            env["NODE_EXTRA_CA_CERTS"] = str(self.ca / CA_FILE)
        return env

    def tls_failed(self):
        try:
            with (proxy.BINDINGS / "proxy.log").open("rb") as handle:
                handle.seek(self.log_offset)
                return b"stealth: client TLS failed" in handle.read()
        except OSError:
            return False

    def close(self):
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        try:
            self.payload.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def launch(client, command, payload, workspace, *, provider, describe, runner=None, env=None):
    """Run `command` in stealth mode when selected, else call provider().

    describe(port) returns the `Remote KB:` line (without the mode suffix).
    """
    selection = select(client, env)
    if selection.mode == "provider":
        return provider()
    try:
        session = Session(selection.mitmdump, payload, env)
    except StealthError as error:
        if selection.explicit:
            raise ValueError(f"stealth 方式を開始できません: {error}") from error
        notice(f"Remote KB: provider mode (stealth proxy failed: {error})")
        return provider()
    with session:
        notice(f"{describe(session.port)} / stealth")
        try:
            return proxy.run_client(command, session.client_env(client), workspace, runner)
        finally:
            if session.tls_failed():
                notice(TLS_HINT)


# --- kb ca-setup ----------------------------------------------------------

def ensure_ca(mitmdump, env=None):
    """Start mitmdump once so it generates its CA in the kb CA dir."""
    path = ca_dir(env) / CA_FILE
    if path.exists() or not mitmdump:
        return path
    with Session(mitmdump, {"client": None}, env):
        pass
    return path


def ca_setup(env=None, out=None, runner=subprocess.run, platform=sys.platform):
    out = out or sys.stdout
    mitmdump = find_mitmdump(env)
    try:
        pem = ensure_ca(mitmdump, env)
    except StealthError as error:
        pem = ca_dir(env) / CA_FILE
        print(f"CA の生成に失敗しました: {error}", file=out)
    print(f"mitmdump: {mitmdump or '見つかりません（uv tool install mitmproxy、または KB_MITMDUMP を指定）'}", file=out)
    print(f"CA: {pem}{'' if pem.exists() else '（未生成: mitmdump の初回起動で作られます）'}", file=out)
    print("Claude Code は NODE_EXTRA_CA_CERTS で kb が自動指定します。Codex は OS の信頼登録が必要です。", file=out)
    if platform == "darwin":
        keychain = Path.home() / "Library/Keychains/login.keychain-db"
        trusted = None
        if pem.exists():
            try:
                trusted = runner(["security", "verify-cert", "-c", str(pem), "-p", "ssl"],
                                 capture_output=True).returncode == 0
            except OSError:
                trusted = None
        print(f"信頼状態: {'登録済み' if trusted else '未登録' if trusted is False else '不明'}", file=out)
        print("登録コマンド（macOS）:", file=out)
        print(f"  security add-trusted-cert -d -r trustRoot -k {keychain} {pem}", file=out)
    else:
        print("登録手順（Linux）:", file=out)
        print(f"  sudo cp {pem} /usr/local/share/ca-certificates/kb-mitmproxy.crt && sudo update-ca-certificates",
              file=out)
        print(f"  または: sudo trust anchor {pem}", file=out)
    return 0
