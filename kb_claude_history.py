"""Seed a new local Claude session with a KB-shaped user history entry.

This path deliberately does not intercept network traffic or modify an existing
session. Claude owns the transcript after it resumes the newly created session.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

import kb_resume
from kb_claude_remote import kb_block


def seed_new_session(workspace, context, *, session_id=None, env=None):
    """Write an exclusive private transcript and return (session ID, path).

    The saved KB is plaintext conversation history, just as the normal Claude
    transcript is. Never overwrite an existing session or follow its symlink.
    """
    workspace = Path(workspace).expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError(f"not a workspace directory: {workspace}")
    sid = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
    directory = kb_resume.claude_projects_dir(workspace, env)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError(f"invalid Claude project directory: {directory}")
    path = directory / f"{sid}.jsonl"
    entry = {
        "parentUuid": None,
        "isSidechain": False,
        "type": "user",
        "message": {"role": "user", "content": kb_block(context)["text"]},
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cwd": str(workspace),
        "sessionId": sid,
        "userType": "external",
    }
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            fd = None
            json.dump(entry, output, ensure_ascii=False)
            output.write("\n")
    except BaseException:
        if fd is not None:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise
    return sid, path


def require_existing_seed(workspace, session_id, context, *, env=None):
    """Allow resuming only a session whose KB seed is still present and unchanged.

    Never edit an existing transcript: doing so could race an active Claude
    process, and compacted histories may no longer include the original seed.
    """
    sid = str(uuid.UUID(session_id))
    directory = kb_resume.claude_projects_dir(Path(workspace).expanduser().resolve(strict=True), env)
    path = directory / f"{sid}.jsonl"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Claude session transcript is missing or is a symlink")
    entries = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                entries.append(json.loads(line))
    expected = kb_block(context)["text"]
    # The file can contain abandoned branches. Only a seed on the active
    # conversation's ancestry is evidence that Claude will restore it.
    # Claude chains assistant/user messages through attachment and other
    # transcript entries as well, so traverse every parent-bearing node.
    nodes = [entry for entry in entries if isinstance(entry.get("uuid"), str)
             and "parentUuid" in entry and not entry.get("isSidechain")]
    by_id = {entry["uuid"]: entry for entry in nodes}
    current = nodes[-1] if nodes else None
    seen = set()
    found = False
    while current is not None and current["uuid"] not in seen:
        seen.add(current["uuid"])
        if current.get("type") == "user" and current.get("message", {}).get("content") == expected:
            found = True
            break
        parent = current.get("parentUuid")
        current = by_id.get(parent) if parent else None
    # A compacted transcript may still retain the original seed on disk while
    # Claude sends only its summary. Fail closed rather than claim full context.
    compacted = any(entry.get("subtype") == "compact_boundary" or entry.get("type") == "compacted"
                    for entry in entries)
    if not found or compacted:
        raise ValueError("Cannot safely resume with this KB: seed missing, changed, or session compacted. "
                         "Start a new KB session; the original session was not modified.")