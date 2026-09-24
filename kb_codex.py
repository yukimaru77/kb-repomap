"""Small Codex app-server client, adapted from the MIT-licensed KB CLI.

Copyright (c) 2026 GMO Pepabo, Inc. See LICENSE.kb-cli.
"""
import json
import os
import subprocess
from kb_items import guidance_item, load_session_items


def config_flags(overrides=()):
    """Process-scoped options from pool-rr, followed by the caller's options."""
    inherited = json.loads(os.environ.get("KB_CODEX_CONFIG_OVERRIDES", "[]"))
    if not isinstance(inherited, list) or any(not isinstance(value, str) or "=" not in value for value in inherited):
        raise ValueError("KB_CODEX_CONFIG_OVERRIDES must be a JSON array of key=value strings")
    return [part for value in [*inherited, *overrides] for part in ("-c", value)]


class CodexAppServer:
    def __init__(self, overrides=()):
        self.process = subprocess.Popen(
            ["codex", "app-server", *config_flags(overrides), "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1,
        )
        self.next_id = 0
        self.request("initialize", {
            "clientInfo": {"name": "kb_repomap", "version": "0.2.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.send({"method": "initialized", "params": {}})

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def read(self):
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Codex app-server exited: {self.process.poll()}")
        return json.loads(line)

    def request(self, method, params, observe=None):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        while True:
            message = self.read()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message.get("result", {})
            if observe:
                observe(message)

    def start(self, workspace, full_access):
        params = {"cwd": str(workspace), "ephemeral": False}
        if full_access:
            params.update(sandbox="danger-full-access", approvalPolicy="never")
        return self.request("thread/start", params)["thread"]["id"]

    def resume(self, session_id, workspace, full_access):
        params = {"threadId": session_id, "cwd": str(workspace)}
        if full_access:
            params.update(sandbox="danger-full-access", approvalPolicy="never")
        return self.request("thread/resume", params)["thread"]["id"]

    def run_turn(self, session_id, prompt, workspace):
        completed = None
        answer = ""

        def observe(message):
            nonlocal completed, answer
            params = message.get("params", {})
            if params.get("threadId") != session_id:
                return
            if message.get("method") == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage":
                    answer = item.get("text", "")
            if message.get("method") == "turn/completed":
                completed = params["turn"]

        self.request("turn/start", {"threadId": session_id, "cwd": str(workspace),
                     "input": [{"type": "text", "text": prompt}]}, observe)
        while completed is None:
            observe(self.read())
        if completed.get("status") != "completed":
            raise RuntimeError(f"Codex turn failed: {completed}")
        return answer

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()


def start_session(jsonl, workspace, name, update_context, *, full_access=True, prompt=None, remote=None,
                  run_initial_turn=None, overrides=(), developer_text=None):
    # Seeding a resumable session does not itself constitute a user request.
    run_initial_turn = prompt is not None and run_initial_turn is not False
    context_items = []
    if update_context and not run_initial_turn:
        context_items.append({"type": "message", "role": "developer", "content": [
            {"type": "input_text", "text": update_context}]})
    # Validate before starting Codex. Never import a producer's session metadata.
    items = [*load_session_items(jsonl, developer_text=developer_text), *context_items] if remote is None else None
    if remote is not None:
        # thread/start may open a prewarm socket before returning its ID.
        # Register, persist the new empty thread, then close this app-server so
        # the first user turn opens a socket with the binding already in place.
        with (CodexAppServer(overrides) if overrides else CodexAppServer()) as bootstrap:
            session_id = bootstrap.start(workspace, full_access)
            remote.bind(session_id, include_guidance=False)
            # Naming an empty thread only updates its index; it does not create
            # a rollout. Persist the developer guidance through the native
            # history API; the binding omits it to avoid injecting it twice.
            bootstrap.request("thread/inject_items", {
                "threadId": session_id,
                "items": [guidance_item(), *context_items],
            })
            bootstrap.request("thread/name/set", {"threadId": session_id, "name": f"{name} Remote KBを活用する"})
        if not run_initial_turn:
            return session_id
    with (CodexAppServer(overrides) if overrides else CodexAppServer()) as server:
        if remote is None:
            session_id = server.start(workspace, full_access)
            server.request("thread/inject_items", {"threadId": session_id, "items": items})
        else:
            server.resume(session_id, workspace, full_access)
        # The new session uses the installed Codex and the caller's local config.
        if run_initial_turn:
            first_turn = "\n\n".join(part for part in (update_context, prompt) if part)
            server.run_turn(session_id, first_turn, workspace)
        server.request("thread/name/set", {"threadId": session_id, "name": f"{name} KBを活用する"})
    return session_id
