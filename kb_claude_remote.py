"""Run Claude Code through a loopback proxy that injects the decrypted KB."""
import os
from pathlib import Path
import uuid

import kb_claude
import kb_resume
import kb_remote_proxy as proxy
import kb_store as store


PREAMBLE = (
    "The following is the user's portable KB context. Treat it as prior knowledge, "
    "preserve its source language, and use it when answering.\n\n"
)
DEFAULT_UPSTREAM = "https://api.anthropic.com"


RequestError = proxy.RequestError


def inject(body, block):
    """Return the messages request body with the KB block before the last system block."""
    return proxy.inject_claude(body, block)


def make_server(block, upstream=None):
    """Create (but do not start) the injecting proxy on a random loopback port."""
    return proxy.make_server(proxy.claude_injector(block),
                             upstream or os.environ.get("KB_CLAUDE_UPSTREAM", DEFAULT_UPSTREAM))


def start_remote(args, config, runner=None):
    if any(token == "--session-id" or token.startswith("--session-id=") for token in args.claude_args):
        raise ValueError("--session-id はkbが作成するため、Claude側では指定しないでください")
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    context = kb_claude.build_context(loaded, args.name, args.file)
    block = {"type": "text", "text": PREAMBLE + context}
    session_id = str(uuid.uuid4())
    workspace = Path(args.workspace).expanduser().resolve()
    record = {"name": args.name, "store": loaded["store"]["name"], "file": args.file}
    # kb owns the Claude session id, so the binding exists before the first request.
    proxy.write_binding(session_id, "claude", record["name"], record["store"], record["file"], source="kb --session-id")
    binder = kb_resume.ClaudeBinder(config, record, block)
    return kb_resume.run_claude(["--session-id", session_id, *args.claude_args], binder, workspace,
                                session_label=f"KB: {args.name}/{args.file} / Claude session: {session_id}",
                                runner=runner)
