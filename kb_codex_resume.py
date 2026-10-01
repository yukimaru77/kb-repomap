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
import fcntl
import json
import os
from pathlib import Path
import time

from kb_api import COMPACTION_TYPES
from kb_fork_mint import CHARTER
from kb_items import DEFAULT_DEVELOPER_TEXT, REPO_MAP_HEADER


# Origin of threads kb itself creates through the app-server (clientInfo.name).
KB_ORIGINATOR = "kb_repomap"
# codex_prompts::SUMMARY_PREFIX: local compaction stores its summary as a user message.
SUMMARY_PREFIX = "Another language model started to solve this problem"
MAX_FORK_DEPTH = 64
LOCK_WAIT_SECONDS = 15


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


# The bundle Codex re-installs inside a compaction's replacement_history
# (build_initial_context); the new thread gets a fresh one at its start instead.
BUNDLE_KIND_PREFIXES = ("host_skills.", "skills.", "permissions.", "collaboration_mode.", "multi_agent.",
                        "agents_md.", "environments.", "apps.", "plugins.")
BUNDLE_DEVELOPER_PREFIXES = ("<permissions instructions>", "<skills_instructions>", "<multi_agent_role>",
                             "<multi_agent_mode>", "<collaboration_mode>", "<apps_instructions>",
                             "<plugins_instructions>", "<environments_instructions>")


def _initial_context_bundle(item):
    kinds = _kinds(item)
    if not (_message(item, "developer") or _message(item, "user")):
        return False
    if kinds:
        return all(kind.startswith(BUNDLE_KIND_PREFIXES) for kind in kinds)
    prefixes = BUNDLE_DEVELOPER_PREFIXES if item.get("role") == "developer" else INITIAL_CONTEXT_PREFIXES
    return _text(item).lstrip().startswith(prefixes)


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
    """The model-visible history Codex rebuilds on resume: last compaction wins.

    The stale initial-context bundle inside a replacement_history is left out:
    Codex writes the current initial context at the start of the new thread.
    """
    history = []
    for record in records:
        kind, payload = record.get("type"), record.get("payload") or {}
        if kind == "compacted":
            replacement = payload.get("replacement_history")
            if replacement is None:
                raise ValueError("replacement_history の無い旧形式の compaction は変換できません。"
                                 "kb NAME --remote codex ... を使用してください")
            history = [dict(item) for item in replacement if not _initial_context_bundle(item)]
        elif kind == "response_item":
            history.append(payload)
        elif kind == "event_msg" and payload.get("type") == "thread_rolled_back":
            history = _drop_last_turns(history, int(payload.get("num_turns") or 0))
    return history


# EventMsg variants that ThreadHistoryBuilder::handle_event renders into the
# transcript. Rollout reconstruction reads none of them into model history; it
# only uses turn boundaries (task_started/task_complete/turn_aborted/user_message)
# for rollback counting and settings, which the copied turns do not carry.
DISPLAY_EVENTS = frozenset((
    "user_message", "agent_message", "agent_reasoning", "agent_reasoning_raw_content",
    "web_search_begin", "web_search_end", "exec_command_begin", "exec_command_end",
    "guardian_assessment", "apply_patch_approval_request", "patch_apply_begin", "patch_apply_end",
    "dynamic_tool_call_request", "dynamic_tool_call_response", "mcp_tool_call_begin",
    "mcp_tool_call_end", "view_image_tool_call", "image_generation_begin", "image_generation_end",
    "collab_agent_spawn_begin", "collab_agent_spawn_end", "collab_agent_interaction_begin",
    "collab_agent_interaction_end", "sub_agent_activity", "collab_waiting_begin",
    "collab_waiting_end", "collab_close_begin", "collab_close_end", "collab_resume_begin",
    "collab_resume_end", "context_compacted", "entered_review_mode", "exited_review_mode",
    "item_started", "item_completed", "error", "turn_aborted", "task_started", "task_complete",
))


def display_events(records):
    """The original thread's transcript events, with rolled-back turns already removed.

    thread_rolled_back itself is never copied: replayed into the new thread it
    would also drop turns from the model-visible history.
    """
    turns = []
    for record in records:
        payload = record.get("payload") or {}
        if record.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "thread_rolled_back":
            # ThreadHistoryBuilder::handle_thread_rollback drops the last N turns.
            turns = turns[:max(len(turns) - int(payload.get("num_turns") or 0), 0)]
        elif kind in DISPLAY_EVENTS:
            if kind == "task_started" or not turns:
                turns.append([])
            turns[-1].append({"timestamp": record.get("timestamp"), "payload": payload})
    return [event for turn in turns for event in turn]


def codex_home():
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def append_events(path, thread_id, events, home=None):
    """Append display-only event_msg records to a rollout no Codex process is writing.

    Holds the thread's writer lock (rollout/src/writer_lock.rs: an exclusive
    flock on thread-writer-locks/<id>.lock, created under .coordination.lock)
    and continues the paginated ordinal sequence that Codex reads back.
    """
    if not events:
        return
    locks = (home or codex_home()) / "thread-writer-locks"
    locks.mkdir(parents=True, exist_ok=True)
    # The seeding app-server may still be exiting behind a launcher wrapper.
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        with (locks / ".coordination.lock").open("a+") as coordination:
            fcntl.flock(coordination, fcntl.LOCK_EX)
            writer = (locks / f"{thread_id}.lock").open("a+")
            try:
                fcntl.flock(writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                writer.close()
        if time.monotonic() > deadline:
            raise ValueError(f"Codex セッション {thread_id} は別のプロセスが書き込み中です")
        time.sleep(0.2)
    with writer:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        ordinal = json.loads(next(line for line in reversed(lines) if line.strip())).get("ordinal")
        output = []
        for event in events:
            record = {"timestamp": event["timestamp"], "type": "event_msg", "payload": event["payload"]}
            if ordinal is not None:
                ordinal += 1
                record = {"timestamp": event["timestamp"], "ordinal": ordinal, **record}
            output.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        with Path(path).open("a", encoding="utf-8") as rollout:
            rollout.write("".join(output))
            rollout.flush()
            os.fsync(rollout.fileno())


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
    """Create the converted thread on `server`.

    Returns (original id, new id, new rollout path, display events). The caller
    appends the events with append_events once this app-server has exited.
    """
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
    path = _thread(server, session_id)["path"]
    return source["id"], session_id, path, display_events(records)
