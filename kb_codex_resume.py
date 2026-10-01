"""Convert an existing Codex thread into a new local-KB thread (`kb NAME codex resume ID`).

The original thread is left untouched. Codex creates the new thread itself
(thread/start + thread/inject_items), so it owns the rollout file and the
thread index; kb only decides which model-visible items go into it:

    [fresh initial context (Codex)] + KB items + original effective history

The effective history mirrors Codex's rollout reconstruction: the newest
`compacted` record's replacement_history (only the last compaction counts)
followed by the response items recorded after it, or every response item when
there was no compaction. Paginated forks keep their parent's prefix behind
`session_meta.history_base`, which is followed here.
"""
import json
from pathlib import Path

from kb_api import COMPACTION_TYPES
from kb_fork_mint import CHARTER
from kb_items import DEFAULT_DEVELOPER_TEXT, REPO_MAP_HEADER


# Origin of threads kb itself creates through the app-server (clientInfo.name).
KB_ORIGINATOR = "kb_repomap"
# codex_prompts::SUMMARY_PREFIX: local compaction stores its summary as a user message.
SUMMARY_PREFIX = "Another language model started to solve this problem"
MAX_FORK_DEPTH = 64


def _text(item):
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content or [] if isinstance(part, dict))


def _message(item, role):
    return item.get("type", "message") == "message" and item.get("role") == role


# Text prefixes of Codex's user-role context fragments, for items without kind metadata.
CONTEXT_PREFIXES = ("# AGENTS.md instructions", "<user_instructions>", "<environment_context>",
                    "<environments_", "<skill", "<user_shell_command>", "<turn_aborted>",
                    "<subagent_notification>", "<codex_internal_context", "<monitor_notification>",
                    "<external_", "<goal_context>", "<startup_context>")
# The initial-context subset that Codex writes afresh for the new thread.
INITIAL_CONTEXT_PREFIXES = CONTEXT_PREFIXES[:3]
INITIAL_CONTEXT_KINDS = ("agents_md.", "environments.")


def _kinds(item):
    metadata = item.get("internal_chat_message_metadata_passthrough") or {}
    return metadata.get("content_item_kinds") or []


def _initial_context_user(item):
    kinds = _kinds(item)
    if kinds:
        return all(kind.startswith(INITIAL_CONTEXT_KINDS) for kind in kinds)
    return _text(item).lstrip().startswith(INITIAL_CONTEXT_PREFIXES)


def is_user_turn(item):
    """Approximates codex core's is_user_turn_boundary for rollback replay."""
    if item.get("type") == "agent_message":
        return True
    if not _message(item, "user") or _text(item).startswith(SUMMARY_PREFIX):
        return False
    kinds = _kinds(item)
    if kinds:
        return any(kind.startswith("user.") for kind in kinds)
    return not _text(item).lstrip().startswith(CONTEXT_PREFIXES)


def read_rollout(path, locate, end=None, depth=0):
    """Return a thread's logical rollout records, including an inherited history_base prefix.

    `locate(thread_id)` returns the rollout path of another thread.
    """
    if depth > MAX_FORK_DEPTH:
        raise ValueError("Codex のフォーク履歴が深すぎます")
    records = []
    with Path(path).open(encoding="utf-8") as rollout:
        for line in rollout:
            if not line.strip():
                continue
            record = json.loads(line)
            if end is not None and record.get("ordinal", 0) >= end:
                break
            records.append(record)
    meta = records[0].get("payload", {}) if records and records[0].get("type") == "session_meta" else {}
    base = meta.get("history_base")
    if not base:
        return records
    prefix = read_rollout(locate(base["thread_id"]), locate, base["end_ordinal_exclusive"], depth + 1)
    return [*prefix, *records]


def _drop_last_turns(history, turns):
    positions = [index for index, item in enumerate(history) if is_user_turn(item)]
    if turns <= 0 or not positions:
        return history
    return history[:positions[max(len(positions) - turns, 0)]]


def effective_history(records):
    """The model-visible history Codex rebuilds on resume: last compaction wins."""
    history = []
    for record in records:
        kind, payload = record.get("type"), record.get("payload") or {}
        if kind == "compacted":
            replacement = payload.get("replacement_history")
            if replacement is None:
                raise ValueError("replacement_history の無い旧形式の compaction は変換できません。"
                                 "kb NAME --remote codex ... を使用してください")
            history = [dict(item) for item in replacement]
        elif kind == "response_item":
            history.append(payload)
        elif kind == "event_msg" and payload.get("type") == "thread_rolled_back":
            history = _drop_last_turns(history, int(payload.get("num_turns") or 0))
    return history


def kb_markers(kb_items):
    """The blobs and message texts by which an earlier copy of this KB is recognized."""
    blobs = {item.get("encrypted_content") for item in kb_items if item.get("type") in COMPACTION_TYPES}
    texts = {_text(item) for item in kb_items if item.get("type", "message") == "message"}
    from kb_codex import REMOTE_SEED_TEXT
    return blobs - {None}, texts | {DEFAULT_DEVELOPER_TEXT, REMOTE_SEED_TEXT, CHARTER}


def _kb_user(item, texts, kb_thread):
    text = _text(item)
    return text in texts or (kb_thread and text.startswith(REPO_MAP_HEADER))


def convert(history, kb_items, *, kb_thread=False):
    """Return the items to inject after Codex's fresh initial context.

    The leading pre-conversation run of `history` (initial context, and any KB
    material of an earlier kb session) is replaced: Codex rewrites the initial
    context and the current KB goes right after it. Everything from the first
    conversation item on is kept verbatim, so nothing is inserted inside, or
    before, what a compaction replaced.
    """
    blobs, texts = kb_markers(kb_items)
    kept = []
    start = 0
    for start, item in enumerate(history):
        if item.get("type") in COMPACTION_TYPES:
            if not (kb_thread or item.get("encrypted_content") in blobs):
                break  # an unknown leading blob is conversation state, not ours to drop
        elif _message(item, "developer"):
            text = _text(item)
            if not (kb_thread or text.lstrip().startswith("<") or text in texts):
                kept.append(item)
        elif _message(item, "user") and (_initial_context_user(item) or _kb_user(item, texts, kb_thread)):
            pass
        else:
            break
    else:
        start = len(history)
    return [*kept, *kb_items, *history[start:]]


def _thread(server, thread_id):
    return server.request("thread/read", {"threadId": thread_id})["thread"]


def latest_thread(server, workspace, all_cwds=False):
    params = {"limit": 1, "sortKey": "updated_at"}
    if not all_cwds:
        params["cwd"] = str(workspace)
    data = server.request("thread/list", params).get("data") or []
    if not data:
        raise ValueError("再開できる Codex セッションがありません")
    return data[0]["id"]


def resume_session(server, kb_items, name, request, workspace, *, explicit_model=False):
    """Create the converted thread on `server`; return (original id, new id)."""
    original = request["thread"] or latest_thread(server, workspace, request["all"])
    source = _thread(server, original)
    if not source.get("path"):
        raise ValueError(f"Codex セッション {original} のロールアウトが見つかりません")
    records = read_rollout(source["path"], lambda thread_id: _thread(server, thread_id)["path"])
    items = convert(effective_history(records), kb_items,
                    kb_thread=source.get("originator") == KB_ORIGINATOR)
    params = {"cwd": source.get("cwd") or str(workspace), "ephemeral": False}
    if not explicit_model and source.get("model"):
        # Keep the original model so its encrypted reasoning stays usable.
        params["model"] = source["model"]
        if source.get("reasoningEffort"):
            params["config"] = {"model_reasoning_effort": source["reasoningEffort"]}
    session_id = server.request("thread/start", params)["thread"]["id"]
    server.request("thread/inject_items", {"threadId": session_id, "items": items})
    label = source.get("name") or original
    server.request("thread/name/set", {"threadId": session_id, "name": f"{name} KBを活用する ← {label}"})
    return source["id"], session_id
