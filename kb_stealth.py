"""Stealth remote KB: run one client behind a per-session mitmdump.

The client keeps its own settings, provider, base URL and login; only
HTTPS_PROXY (and, for Claude Code, NODE_EXTRA_CA_CERTS) is added to its
environment. kb_stealth_addon.py edits the model requests inside mitmdump.
"""
import contextlib
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

import kb_remote_proxy as proxy


ADDON = Path(__file__).resolve().with_name("kb_stealth_addon.py")
REPO = ADDON.parent
ALLOW_HOSTS = r"^(chatgpt\.com|api\.anthropic\.com):443$"
CA_FILE = "mitmproxy-ca-cert.pem"
READY_TIMEOUT = 10.0
MODES = ("provider", "stealth")
NOT_FOUND = ("Remote KB: mitmdump が見つかりません。--remote はクライアント設定を変えないステルス方式でのみ動作します。\n"
             "  導入: uv tool install mitmproxy && kb ca-setup")
PROVIDER_HINT = "旧 provider 方式を明示的に使う場合のみ（Claude Code）: KB_REMOTE_MODE=provider"
CODEX_STEALTH_ONLY = "Remote KB: Codex の --remote はステルス方式のみです（KB_REMOTE_MODE=provider は Claude Code 専用）"
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
    # `uv tool install mitmproxy` (what install.sh runs) puts it here even when
    # ~/.local/bin is not on PATH yet.
    home = Path(env.get("HOME") or Path.home())
    candidate = home / ".local/bin/mitmdump"
    return str(candidate) if os.access(candidate, os.X_OK) else None


def ca_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("KB_CA_DIR") or Path.home() / ".cache/kb/ca").expanduser()


@dataclass
class Selection:
    mode: str
    mitmdump: str = None
    explicit: bool = False


def select(client, env=None):
    """Pick stealth or (Claude Code only) provider mode for one --remote session."""
    env = os.environ if env is None else env
    wanted = (env.get("KB_REMOTE_MODE") or "").strip().lower()
    if wanted and wanted not in MODES:
        raise ValueError(f"KB_REMOTE_MODE は provider か stealth です: {wanted}")
    if wanted == "provider":
        if client == "codex":
            raise ValueError(CODEX_STEALTH_ONLY)
        return Selection("provider", explicit=True)
    mitmdump = find_mitmdump(env)
    if mitmdump is None:
        # No silent fallback: provider mode changes what the client sees, so
        # it must be chosen explicitly with KB_REMOTE_MODE=provider.
        raise ValueError(NOT_FOUND if client == "codex" else f"{NOT_FOUND}\n  {PROVIDER_HINT}")
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
                "--set", f"kb_bindings={proxy.BINDINGS}", "--set", f"kb_repo={REPO}",
                "--set", f"kb_parent={os.getpid()}"]

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


@contextlib.contextmanager
def stealth_session(client, payload, env=None, selection=None):
    """Run one mitmdump for `client` (a Session) while the block is active."""
    selection = selection or select(client, env)
    try:
        session = Session(selection.mitmdump, payload, env)
    except StealthError as error:
        hint = "" if client == "codex" else f"\n  {PROVIDER_HINT}"
        raise ValueError(f"stealth 方式を開始できません: {error}\n  CA/導入の確認: kb ca-setup{hint}") from error
    with session, _exit_on_termination():
        yield session


# Interactive Codex (and resume/fork) normally hands its model traffic to the
# shared app-server daemon, a separate long-lived process that did not inherit
# this session's HTTPS_PROXY, so the KB would silently not be injected.
# --no-daemon keeps the traffic in the process kb launched. exec/review never
# use the daemon and do not accept the flag.
_DAEMON_SUBCOMMANDS = ("resume", "fork")


def codex_without_daemon(command):
    """Return the codex argv with --no-daemon where Codex would use its daemon."""
    if not command or command[0] != "codex" or "--no-daemon" in command:
        return list(command)
    args = command[1:]
    from kb_native_local import subcommand_index
    index = subcommand_index(args)
    if index is None:
        return ["codex", "--no-daemon", *args]  # interactive TUI
    if args[index] in _DAEMON_SUBCOMMANDS:
        return ["codex", *args[:index + 1], "--no-daemon", *args[index + 1:]]
    return list(command)


def run_client(session, client, command, workspace, runner=None):
    if client == "codex":
        command = codex_without_daemon(command)
    """Run the client behind `session`, then point at kb ca-setup if its TLS failed."""
    try:
        return proxy.run_client(command, session.client_env(client), workspace, runner)
    finally:
        if session.tls_failed():
            notice(TLS_HINT)


def launch(client, command, payload, workspace, *, describe, provider=None, runner=None, env=None):
    """Run `command` in stealth mode when selected, else call provider() (Claude Code only).

    describe(port) returns the `Remote KB:` line (without the mode suffix).
    """
    selection = select(client, env)
    if selection.mode == "provider":
        return provider()
    with stealth_session(client, payload, env, selection) as session:
        notice(f"{describe(session.port)} / stealth")
        return run_client(session, client, command, workspace, runner)


class _exit_on_termination:
    """Turn SIGTERM/SIGHUP into SystemExit so the session's cleanup runs.

    mitmdump runs in its own session (Ctrl-C belongs to the client), so it would
    otherwise outlive a kb that is terminated. The addon also exits on its own
    when kb disappears without running any cleanup (kb_parent).
    """

    SIGNALS = (signal.SIGTERM, signal.SIGHUP)

    def __enter__(self):
        self.previous = {}
        if threading.current_thread() is threading.main_thread():
            for number in self.SIGNALS:
                self.previous[number] = signal.signal(number, self._raise)
        return self

    @staticmethod
    def _raise(number, _frame):
        raise SystemExit(128 + number)

    def __exit__(self, *_args):
        for number, handler in self.previous.items():
            signal.signal(number, handler)


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
