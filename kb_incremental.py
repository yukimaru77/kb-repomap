"""Identify reusable first-stage blobs from a published KB."""
import json
import hashlib
from pathlib import Path

from kb_api import COMPACTION_TYPES
from kb_items import load_items


def manifest_from_state(state):
    if state.get("stage2_ids") or state.get("prelude_files"):
        return None
    by_pack = {}
    for blob in state.get("blobs", []):
        if blob.get("type") not in COMPACTION_TYPES or not blob.get("id"):
            return None
        pack_id = state.get("blob_pack_ids", {}).get(blob["id"])
        if not pack_id:
            return None
        by_pack.setdefault(pack_id, []).append(blob["id"])
    packs = []
    for round_ in sorted(state.get("rounds", []), key=lambda item: item.get("plan_order", item["n"])):
        if not round_.get("source", "").startswith("pack-"):
            return None
        ids = by_pack.pop(round_.get("source_id"), None)
        if not ids:
            return None
        packs.append({"files": round_["files"], "blob_ids": ids})
    if by_pack or not packs:
        return None
    if not isinstance(state.get("reading_instructions"), str) or not isinstance(state.get("instructions"), str):
        return None
    return {"format": 1,
            "reading_sha256": hashlib.sha256(state["reading_instructions"].encode()).hexdigest(),
            "instructions_sha256": hashlib.sha256(state["instructions"].encode()).hexdigest(),
            "packs": packs}


def reuse_input(loaded, name, build_root):
    """Use published metadata, or a matching local build of a legacy snapshot."""
    if not loaded or not loaded.get("jsonl"):
        return None
    blobs = [item for item in load_items(loaded["jsonl"])
             if item.get("type") in COMPACTION_TYPES]
    ids = [item.get("id") for item in blobs]
    if not ids or None in ids or len(set(ids)) != len(ids):
        return None
    manifest = loaded["info"].get("pack_manifest")
    if manifest is None:
        parent = build_root.parent
        for path in parent.glob(f"*/{name}/state.json"):
            try:
                state = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if state.get("commit") != loaded["info"].get("source_commit"):
                continue
            if [item.get("id") for item in state.get("blobs", [])] == ids:
                manifest = manifest_from_state(state)
                if manifest:
                    break
    if not isinstance(manifest, dict) or manifest.get("format") != 1:
        return None
    used = [blob_id for pack in manifest.get("packs", []) for blob_id in pack.get("blob_ids", [])]
    if len(used) != len(ids) or set(used) != set(ids):
        return None
    by_id = {item["id"]: item for item in blobs}
    return {"commit": loaded["info"]["source_commit"],
            "reading_sha256": manifest.get("reading_sha256"),
            "instructions_sha256": manifest.get("instructions_sha256"),
            "packs": [{"files": pack["files"], "blobs": [by_id[blob_id] for blob_id in pack["blob_ids"]]}
                      for pack in manifest["packs"]]}
