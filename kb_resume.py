"""Session bindings for --remote sessions and resuming them through kb.

kb writes ~/.cache/kb/bindings/<session-id>.json for every --remote session it
starts. `kb --remote codex resume ...` and `kb --remote claude --resume ...`
look the session up and inject the *current* contents of the bound KB again.
"""
import json
import os
from pathlib import Path
import re
import sys
import threading
from types import SimpleNamespace

import kb_remote_proxy as proxy
import kb_store as store


UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def notice(message):
    print(message, file=sys.stderr, flush=True)


# --- argv -----------------------------------------------------------------

def codex_resume_id(native_args):
    """Return (is a resume, explicit session id or None) for native Codex arguments."""
    options = native_args[:native_args.index("--")] if "--" in native_args else native_args
    if "resume" not in options:
        return False, None
    for token in native_args[native_args.index("resume") + 1:]:
        if UUID.match(token):
            return True, token
    return True, None


def claude_resume_id(claude_args):
    """Return (is a resume, explicit session id or None) for Claude Code arguments."""
    for index, token in enumerate(claude_args):
        if token == "--":
            break
        if token in ("--resume", "-r"):
            following = claude_args[index + 1] if index + 1 < len(claude_args) else ""
            return True, following if UUID.match(following) else None
        if token.startswith("--resume="):
            value = token.split("=", 1)[1]
            return True, value if UUID.match(value) else None
        if token in ("--continue", "-c"):
            return True, None
    return False, None


# --- KB material from a binding -----------------------------------------

def _loaded(config, record):
    return store.find_kb(config, record["name"], record.get("store"), filename=record["file"])


def codex_items(config, record):
    """Current KB items for a Codex binding (latest store contents, current source diff)."""
    from kb_remote import RemoteKB
    loaded = _loaded(config, record)
    developer = loaded.get("developer_text")
    remote = RemoteKB(config, loaded["jsonl"],
                      developer_text=developer if developer and developer.strip() else None)
    # Old bindings may carry `local_guidance` (their thread persisted the former
    # hard-coded guidance). The items no longer contain it, so inject all of them.
    items = remote.items
    info = loaded["info"]
    if info.get("source_kind") != "paper" and info.get("repository_url") and info.get("source_commit"):
        try:
            _head, context = store.source_update(info)
        except Exception as error:  # the KB itself is still usable
            proxy.log(f"source update skipped for {record['name']}: {error}")
            context = ""
        if context:
            items = [*items, {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": context}]}]
    return items


def claude_block(config, record):
    import kb_claude
    from kb_claude_remote import kb_block
    loaded = _loaded(config, record)
    return kb_block(kb_claude.build_context(loaded, record["name"], record["file"]))


# --- Codex ----------------------------------------------------------------

class CodexBinder:
    """Observe Codex requests: bind their session ids and pick the KB items.

    With `record` (new session or explicit resume) the items are fixed up front.
    Without it the first observed session/thread/parent id selects a binding.
    """

    def __init__(self, config, record=None, items=None):
        self.config = config
        self.record = record
        self.cached = items
        self.resolved = record is not None
        self.seen = set()
        self.lock = threading.Lock()

    def observe(self, method, path, headers, body):
        if not proxy.is_codex_responses(method, path):
            return
        try:
            payload = json.loads(proxy.decoded(headers, body))
        except (proxy.RequestError, UnicodeDecodeError, ValueError):
            return
        self.observe_payload(headers, payload)

    def observe_payload(self, headers, payload):
        """Bind the ids of one Responses request (HTTP body or WebSocket response.create)."""
        identity = proxy.codex_identity(headers, payload if isinstance(payload, dict) else {})
        with self.lock:
            if not self.resolved:
                self._resolve(identity)
            if self.record is None:
                return
            for key in ("thread", "session"):
                session_id = identity[key]
                if not session_id or session_id in self.seen:
                    continue
                self.seen.add(session_id)
                source = identity["sources"].get(key)
                try:
                    written = proxy.write_binding(
                        session_id, "codex", self.record["name"], self.record.get("store"), self.record["file"],
                        source=source, overwrite=False)
                    if written:
                        proxy.log(f"codex binding {session_id} ({source}) -> "
                                  f"{self.record['name']}/{self.record['file']}")
                except (OSError, ValueError) as error:
                    proxy.log(f"codex binding {session_id} failed: {error}")

    def _resolve(self, identity):
        self.resolved = True
        for key in ("thread", "session", "parent"):
            session_id = identity[key]
            record = proxy.read_binding(session_id, "codex") if session_id else None
            if record:
                try:
                    self.cached = codex_items(self.config, record)
                except Exception as error:
                    proxy.log(f"codex resume {session_id}: KB unavailable: {error}")
                    return
                self.record = record
                proxy.log(f"codex resume {session_id} ({identity['sources'].get(key)}) -> {record['name']}")
                return
        proxy.log(f"codex resume: no kb binding for {identity.get('thread') or identity.get('session')}; not injecting")

    def items(self, _headers, _payload):
        return self.cached


def resume_codex(native_args, config, *, runner=None):
    import kb_codex
    import kb_native
    from kb_remote import pool_endpoint
    is_resume, session_id = codex_resume_id(native_args)
    if not is_resume:
        raise ValueError("kb --remote codex はセッションの再開専用です: kb --remote codex resume [ID]。"
                         "新規セッションは kb NAME --remote codex ...")
    workspace = Path.cwd()
    binder = CodexBinder(config)
    if session_id:
        record = proxy.read_binding(session_id, "codex")
        if record is None:
            notice(f"kb: {session_id} はkbの --remote セッションではないため、KBを挿入せずに起動します")
            command = kb_native.with_config(native_args, kb_codex.config_flags())
            return proxy.run_client(command, dict(os.environ), workspace, runner)
        notice(f"KB: {record['name']}/{record['file']} (binding {session_id}) の最新内容を挿入して再開します")
        binder = CodexBinder(config, record, codex_items(config, record))
    def remote():
        origin, key = pool_endpoint(config)
        return SimpleNamespace(origin=origin, key=key)

    return kb_native.launch_remote(native_args, binder, workspace, remote, runner=runner)


# --- Claude ---------------------------------------------------------------

class ClaudeBinder:
    def __init__(self, config, record=None, block=None):
        self.config = config
        self.record = record
        self.block = block
        self.resolved = record is not None
        self.seen = set()
        self.lock = threading.Lock()

    def observe(self, method, path, headers, body):
        if not proxy.is_claude_messages(method, path):
            return
        try:
            session_id, source = proxy.claude_identity(headers, proxy.decoded(headers, body))
        except proxy.RequestError:
            return
        with self.lock:
            if not self.resolved:
                self.resolved = True
                record = proxy.read_binding(session_id, "claude") if session_id else None
                if record:
                    try:
                        self.block = claude_block(self.config, record)
                        self.record = record
                        proxy.log(f"claude resume {session_id} ({source}) -> {record['name']}")
                    except Exception as error:
                        proxy.log(f"claude resume {session_id}: KB unavailable: {error}")
                else:
                    proxy.log(f"claude resume: no kb binding for {session_id} ({source}); not injecting")
            if self.record and session_id and session_id not in self.seen:
                # A forked or continued session gets its own id; bind it too.
                self.seen.add(session_id)
                try:
                    proxy.write_binding(session_id, "claude", self.record["name"], self.record.get("store"),
                                        self.record["file"], source=source, overwrite=False)
                except (OSError, ValueError):
                    pass

    def injector(self, method, path, headers, body):
        if not proxy.is_claude_messages(method, path) or self.block is None:
            return None
        return proxy.claude_edit(headers, proxy.decoded(headers, body), self.block)


def run_claude(claude_args, binder, workspace, *, session_label, runner=None):
    """Run Claude Code in stealth mode, or through kb's base-URL proxy (provider mode)."""
    import kb_stealth
    size = len(binder.block["text"].encode("utf-8")) if binder.block else "lazy"

    def describe(port):
        label = session_label
        if os.environ.get("ANTHROPIC_BASE_URL"):
            label += (f"\nkb: ANTHROPIC_BASE_URL={os.environ['ANTHROPIC_BASE_URL']} のため、"
                      "KBは api.anthropic.com 宛ての要求にだけ挿入されます")
        return f"{label}\nRemote KB: proxy 127.0.0.1:{port} / {size} bytes"

    payload = {"client": "claude", "record": binder.record, "block": binder.block}
    return kb_stealth.launch("claude", ["claude", *claude_args], payload, workspace,
                             provider=lambda: run_claude_provider(claude_args, binder, workspace,
                                                                  session_label=session_label, runner=runner),
                             describe=describe, runner=runner)


def run_claude_provider(claude_args, binder, workspace, *, session_label, runner=None):
    import kb_claude_remote
    upstream = os.environ.get("KB_CLAUDE_UPSTREAM", kb_claude_remote.DEFAULT_UPSTREAM)
    with proxy.start(binder.injector, upstream, on_request=binder.observe) as running:
        env = dict(os.environ)
        if env.get("ANTHROPIC_BASE_URL"):
            notice(f"kb: ANTHROPIC_BASE_URL={env['ANTHROPIC_BASE_URL']} はこのセッションではkbのプロキシで上書きします")
        env["ANTHROPIC_BASE_URL"] = running.url
        # Claude Code disables MCP tool search behind non-Anthropic base URLs,
        # which would inline every MCP tool schema (~160k tokens) into each
        # request. The kb proxy forwards tool_reference blocks unchanged, so
        # keep the on-demand loading the user gets without the proxy.
        env.setdefault("ENABLE_TOOL_SEARCH", "true")
        size = len(binder.block["text"].encode("utf-8")) if binder.block else "lazy"
        notice(f"{session_label}\nRemote KB: proxy 127.0.0.1:{running.port} / {size} bytes / provider")
        return proxy.run_client(["claude", *claude_args], env, workspace, runner)


def resume_claude(claude_args, config, *, runner=None):
    is_resume, session_id = claude_resume_id(claude_args)
    if not is_resume:
        raise ValueError("kb --remote claude はセッションの再開専用です: kb --remote claude --resume ID / --continue。"
                         "新規セッションは kb NAME --remote claude ...")
    workspace = Path.cwd()
    if session_id:
        record = proxy.read_binding(session_id, "claude")
        if record is None:
            notice(f"kb: {session_id} はkbの --remote セッションではないため、KBを挿入せずに起動します")
            return proxy.run_client(["claude", *claude_args], dict(os.environ), workspace, runner)
        binder = ClaudeBinder(config, record, claude_block(config, record))
        label = f"KB: {record['name']}/{record['file']} (binding {session_id}) の最新内容を挿入して再開します"
    else:
        binder = ClaudeBinder(config)
        label = "KB: 最初のリクエストのセッションIDからbindingを探します"
    return run_claude(claude_args, binder, workspace, session_label=label, runner=runner)


def main(client, client_args, config):
    options = client_args[:client_args.index("--")] if "--" in client_args else client_args
    if any(value in ("--help", "-h", "--version", "-V") for value in options):
        os.execvp(client, [client, *client_args])
    if client == "codex":
        return resume_codex(client_args, config)
    return resume_claude(client_args, config)
