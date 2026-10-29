"""Portable KB items, independent of any Codex session or local instructions."""
import json
import os
from pathlib import Path

from kb_api import COMPACTION_TYPES
from kb_fork_mint import CHARTER


# Default store-owned dev.txt, written by `kb create` / `kb publish-paper` when
# the KB has none. kb injects no hard-coded guidance; edit dev.txt in the store.
DEFAULT_DEVELOPER_TEXT = (
    "The supplied compacted context is prior knowledge provided by the user.\n"
    "Use it as a foundation for subsequent understanding and work.\n"
    "When precise details are needed, use that knowledge to narrow down relevant\n"
    "sources and search them efficiently.\n"
)


# Heads the user item that carries the current map when a KB reuses older blobs.
REPO_MAP_HEADER = "Current repository map. It supersedes maps inside older reused blobs.\n\n"


# Codex inserts its own (non-encrypted) compaction summary as a user message with this prefix
# (core/src/compact.rs). The KB's plaintext reuses that shape so Codex reads it as prior context.
CODEX_SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary "
    "to assist with your own analysis:")
# Distinguishes the KB summary from a real Codex compaction summary sharing the prefix.
KB_TEXT_HEADER = "## Repository knowledge base (kb)"
KB_TEXT_MARKER = f"{CODEX_SUMMARY_PREFIX}\n{KB_TEXT_HEADER}\n"


def kb_text_item(text):
    """The decrypted KB as one user message shaped like Codex's own compaction summary."""
    return {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": KB_TEXT_MARKER + text.strip() + "\n"}]}


def developer_item(text):
    return {"type": "message", "role": "developer", "content": [
        {"type": "input_text", "text": text}]}


def load_session_items(path, *, developer_text=None, kb_text=None):
    """Return the KB items followed by the store dev.txt (if any), without touching the snapshot.

    With `kb_text` (the published decrypted plaintext of this KB) the compaction blobs are replaced
    by one user message holding that text; other snapshot items keep their place."""
    items = []
    placed = False
    for item in load_items(path):
        if kb_text is not None and item.get("type") in COMPACTION_TYPES:
            if not placed:
                items.append(kb_text_item(kb_text))
                placed = True
            continue
        content = item.get("content")
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content or [] if isinstance(part, dict))
        # The exact legacy charter is superseded by dev.txt.
        # Preserve other user instructions and all opaque compaction fields.
        if item.get("role") == "user" and text == CHARTER:
            continue
        items.append(item)
    if developer_text is not None:
        if not isinstance(developer_text, str):
            raise ValueError("dev.txt must contain UTF-8 text")
        if developer_text.strip():
            # After the compacted context: it reads as guidance on the material above.
            items.append(developer_item(developer_text))
    return items


def validate_items(items):
    if not isinstance(items, list) or not items:
        raise ValueError("KB items must be a nonempty array")
    blobs = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("KB item must be an object")
        if item.get("type") in COMPACTION_TYPES:
            if not isinstance(item.get("encrypted_content"), str) or not item["encrypted_content"]:
                raise ValueError("KB compaction item is missing encrypted_content")
            blobs += 1
        elif item.get("role") == "user" and item.get("type", "message") == "message":
            if not item.get("content"):
                raise ValueError("KB user instruction is empty")
        else:
            raise ValueError("KB items may only contain compaction blobs and user instructions")
    if not blobs:
        raise ValueError("KB contains no encrypted compaction response_item")
    return items


def load_items(path):
    raw = Path(path).read_text(encoding="utf-8")
    if raw.lstrip().startswith("["):
        return validate_items(json.loads(raw))
    # Read old session snapshots without importing their metadata or instructions.
    items = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("type") != "response_item":
            continue
        item = record.get("payload") or {}
        content = item.get("content")
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content or [] if isinstance(part, dict))
        if item.get("type") in COMPACTION_TYPES or (
                item.get("role") == "user" and item.get("type", "message") == "message"
                and text == CHARTER):
            items.append(item)
    if not items:
        raise ValueError("KB contains no encrypted compaction response_item")
    return validate_items(items)


def dump_items(path, items):
    data = json.dumps(validate_items(items), ensure_ascii=False, indent=2) + "\n"
    with Path(path).open("w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(data)
