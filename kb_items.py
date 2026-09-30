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


def developer_item(text):
    return {"type": "message", "role": "developer", "content": [
        {"type": "input_text", "text": text}]}


def load_session_items(path, *, developer_text=None):
    """Return the KB items followed by the store dev.txt (if any), without touching the snapshot."""
    items = []
    for item in load_items(path):
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
