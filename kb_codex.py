"""Small Codex app-server client, adapted from the MIT-licensed KB CLI.

Copyright (c) 2026 GMO Pepabo, Inc. See LICENSE.kb-cli.
"""
import json
import datetime as dt
import os
from pathlib import Path
import subprocess
import uuid


def import_template(jsonl):
    """Import an immutable store snapshot without colliding with local thread IDs."""
    first, separator, rest = Path(jsonl).read_bytes().partition(b"\n")
    meta = json.loads(first)
    now = dt.datetime.now(dt.timezone.utc)
    session_id = str(uuid.UUID(int=(int(now.timestamp() * 1000) << 80)
                              | (0x7 << 76) | (int.from_bytes(os.urandom(2), "big") & 0xFFF) << 64
                              | (0b10 << 62) | (int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1))))
    meta["payload"]["id"] = session_id
    if "session_id" in meta["payload"]:
        meta["payload"]["session_id"] = session_id
    sessions = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "sessions"
    directory = sessions / f"{now:%Y/%m/%d}"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"rollout-{now:%Y-%m-%dT%H-%M-%S}-{session_id}.jsonl"
    with target.open("xb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(json.dumps(meta, ensure_ascii=False).encode() + separator + rest)
    return session_id


class CodexAppServer:
    def __init__(self):
        self.process = subprocess.Popen(
            ["codex", "app-server", "--listen", "stdio://"],
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

    def fork(self, jsonl, workspace, full_access):
        # The store snapshot can share an ID with a locally indexed thread.
        # Import it under a fresh ID and let Codex resolve its native session path.
        params = {"threadId": import_template(jsonl),
                  "cwd": str(workspace), "ephemeral": False}
        if full_access:
            params.update(sandbox="danger-full-access", approvalPolicy="never")
        return self.request("thread/fork", params)["thread"]["id"]

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


def start_session(jsonl, workspace, name, update_context, *, full_access=True, prompt=None, remote=None):
    if remote is not None:
        # thread/start may open a prewarm socket before returning its ID.
        # Register, persist the new empty thread, then close this app-server so
        # the first user turn opens a socket with the binding already in place.
        with CodexAppServer() as bootstrap:
            session_id = bootstrap.start(workspace, full_access)
            remote.bind(session_id)
            bootstrap.request("thread/name/set", {"threadId": session_id, "name": f"{name} Remote KBを活用する"})
    with CodexAppServer() as server:
        if remote is None:
            session_id = server.fork(jsonl, workspace, full_access)
        else:
            server.resume(session_id, workspace, full_access)
        # Submit updates AFTER the fork: an unfinished trailing user message in
        # a synthetic template can be omitted by Codex's fork snapshot boundary.
        instruction = prompt or "今まで読んだ内容をふんだんに活用してください。"
        first_turn = "\n\n".join(part for part in (update_context, instruction) if part)
        server.run_turn(session_id, first_turn, workspace)
        server.request("thread/name/set", {"threadId": session_id, "name": f"{name} KBを活用する"})
    return session_id
