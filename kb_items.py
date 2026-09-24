"""Portable KB items, independent of any Codex session or local instructions."""
import json
import os
from pathlib import Path

from kb_api import COMPACTION_TYPES
from kb_fork_mint import CHARTER


CONTEXT_GUIDANCE = (
    "The supplied compacted context is prior knowledge provided by the user. "
    "Use it as a foundation for subsequent understanding and work. "
    "When precise details are needed, use that knowledge to narrow down relevant "
    "sources and search them efficiently."
)


def guidance_item():
    return {"type": "message", "role": "developer", "content": [
        {"type": "input_text", "text": CONTEXT_GUIDANCE}]}


def load_session_items(path, *, developer_text=None):
    """Attach current guidance without changing the portable snapshot on disk."""
    items = []
    for item in load_items(path):
        content = item.get("content")
        text = content if isinstance(content, str) else "".join(
            part.get("text", "") for part in content or [] if isinstance(part, dict))
        # The exact legacy charter is superseded by the developer guidance.
        # Preserve other user instructions and all opaque compaction fields.
        if item.get("role") == "user" and text == CHARTER:
            continue
        items.append(item)
    instructions = [guidance_item()]
    if developer_text is not None:
        if not isinstance(developer_text, str):
            raise ValueError("dev.txt must contain UTF-8 text")
        if developer_text.strip():
            instructions.append({"type": "message", "role": "developer", "content": [
                {"type": "input_text", "text": developer_text}]})
    return [*instructions, *items]


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
