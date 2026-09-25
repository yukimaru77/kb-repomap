#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git repository -> repo_map-prefixed, directory-related source blobs."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import as_completed, ThreadPoolExecutor
from pathlib import Path, PurePosixPath

import kb_api


SKIP_NAMES = {
    "package-lock.json", "bun.lock", "go.sum", "Cargo.lock", "yarn.lock",
    "pnpm-lock.yaml", "composer.lock", "Gemfile.lock", "tsconfig.tsbuildinfo",
}
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".mp4",
    ".mov", ".zip", ".gz", ".tgz", ".woff", ".woff2", ".ttf", ".pem",
    ".crt", ".key", ".p12", ".sqlite", ".db", ".svg",
}
SKIP_PARTS = {"node_modules", "vendor", "coverage", "dist", "build", ".next", "public"}
GENERATED_SUFFIXES = (".pb.go", ".pb.validate.go", ".connect.go", ".sql.go")
STRUCTURED_DATA_SUFFIXES = {".json", ".yaml", ".yml", ".csv", ".xml"}
WEB_ASSET_SUFFIXES = {".json", ".html", ".htm", ".js", ".css", ".map"}
SNAPSHOT_PARTS = {"cassettes", "fixtures", "snapshots", "recordings"}
STATIC_ASSET_PARTS = {"assets", "static"}
LARGE_STRUCTURED_BYTES = 200_000
LARGE_AUXILIARY_BYTES = 100_000

READ_INSTRUCTION = """以下のファイルをすべて全文読んでよく咀嚼し、以後、これらのファイルに対する様々な作業や質問、検索で使える知識として保持してください。
構造マップはあくまで静的解析に基づく構造情報です。実装と矛盾する場合は実装を正とし、推測を事実として扱わないでください。"""


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, errors="replace"
    ).rstrip("\n")


def tracked_files(repo):
    raw = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"])
    return [PurePosixPath(p.decode(errors="surrogateescape"))
            for p in raw.split(b"\0") if p]


def repomix_candidates(repo):
    """Return Repomix's mechanically selected text files before KB-specific limits."""
    script = Path(__file__).with_name("repomix_candidates.mjs")
    with tempfile.TemporaryDirectory(prefix="kb-repomix-") as work:
        subprocess.run(["node", str(script), str(repo), work], check=True)
        selected = json.loads((Path(work) / "selected.json").read_text())
    return {PurePosixPath(path) for path in selected}


def low_value_text_reason(rel, data):
    """Identify large generated/data artifacts without dropping ordinary source."""
    path = rel.as_posix().lower()
    parts = {part.lower() for part in rel.parts}
    name = rel.name.lower()
    suffix = rel.suffix.lower()
    size = len(data)

    if suffix == ".json" and "lottie" in parts:
        return "data-driven visual asset"
    if size >= LARGE_AUXILIARY_BYTES \
            and parts.intersection(SNAPSHOT_PARTS) \
            and suffix in STRUCTURED_DATA_SUFFIXES:
        return "large test fixture/cassette/snapshot"
    if size >= LARGE_AUXILIARY_BYTES \
            and parts.intersection(STATIC_ASSET_PARTS) \
            and suffix in WEB_ASSET_SUFFIXES:
        return "large static/generated web asset"

    api_description = any(marker in path for marker in ("openapi", "swagger", "redoc"))
    generated_name = any(
        marker in name for marker in ("bundled", "generated", "compiled")
    )
    if api_description and generated_name \
            and suffix in STRUCTURED_DATA_SUFFIXES.union({".html", ".htm"}):
        return "generated/bundled API description"
    if api_description and size >= LARGE_AUXILIARY_BYTES \
            and suffix in {".html", ".htm"}:
        return "generated API documentation"

    if size >= LARGE_STRUCTURED_BYTES and suffix in STRUCTURED_DATA_SUFFIXES:
        return "large structured data/snapshot"
    return None


def skip_reason(rel, data, max_bytes):
    if rel.name in SKIP_NAMES:
        return "lock/dependency/generated snapshot"
    if rel.suffix.lower() in SKIP_SUFFIXES:
        return "binary/asset/certificate suffix"
    if any(part in SKIP_PARTS for part in rel.parts):
        return "generated/vendor tree"
    path = rel.as_posix()
    if path.endswith(GENERATED_SUFFIXES) or "/gen/" in f"/{path}/" \
            or path.startswith("frontend/protobuf/connect/"):
        return "generated source"
    if len(data) > max_bytes:
        return f"oversize>{max_bytes}"
    if b"\0" in data[:8192]:
        return "binary content"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "non-UTF-8"
    low_value_reason = low_value_text_reason(rel, data)
    if low_value_reason:
        return low_value_reason
    return None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_block(path, text):
    return f"===== FILE: {path} =====\n\n{text}\n\n===== END FILE: {path} ====="


def common_directory(paths):
    if not paths:
        return "."
    parents = [p.parent.parts for p in paths]
    common = []
    for columns in zip(*parents):
        if len(set(columns)) != 1:
            break
        common.append(columns[0])
    return "/".join(common) or "."


def build_related_units(paths, block_tokens, capacity):
    """Collapse the deepest complete subtrees that fit in one source capacity."""
    by_dir = {}
    children = {}
    for path in paths:
        by_dir.setdefault(path.parent, []).append(path)
        parent = path.parent
        while parent != PurePosixPath("."):
            children.setdefault(parent.parent, set()).add(parent)
            parent = parent.parent

    def walk(directory):
        descendants = [p for p in paths if p.parent == directory or directory in p.parents]
        total = sum(block_tokens[p] for p in descendants)
        if descendants and total <= capacity:
            return [descendants]
        units = []
        direct = sorted(by_dir.get(directory, []), key=lambda p: p.as_posix())
        if direct:
            current, used = [], 0
            for path in direct:
                size = block_tokens[path]
                if current and used + size > capacity:
                    units.append(current)
                    current, used = [], 0
                current.append(path)
                used += size
            if current:
                units.append(current)
        for child in sorted(children.get(directory, set()), key=lambda p: p.as_posix()):
            units.extend(walk(child))
        return units

    return walk(PurePosixPath("."))


def pack_units(units, block_tokens, capacity):
    """Merge adjacent related units without splitting a directory unit."""
    packs, current, used = [], [], 0
    for unit in units:
        size = sum(block_tokens[p] for p in unit)
        if current and used + size > capacity:
            packs.append(current)
            current, used = [], 0
        current.extend(unit)
        used += size
    if current:
        packs.append(current)
    return packs


def blob_source(repo_map, paths, contents, reading_instructions=READ_INSTRUCTION):
    manifest = "\n".join(f"- {p.as_posix()}" for p in paths)
    bodies = "\n\n".join(file_block(p.as_posix(), contents[p]) for p in paths)
    return f"""===== REPOSITORY MAP =====

{repo_map}

===== END REPOSITORY MAP =====

===== READING INSTRUCTIONS =====

{reading_instructions}

このblobに含まれるファイル:
{manifest}

===== END READING INSTRUCTIONS =====

{bodies}
"""


def prelude_source(repo_map, label, text, reading_instructions=READ_INSTRUCTION):
    path = PurePosixPath("__kb_prelude__") / label
    return blob_source(repo_map, [path], {path: text}, reading_instructions)


def compact_one(label, text, thin_threshold, model, effort, instructions=kb_api.CHARTER):
    items = [kb_api.u(text)]
    out, secs, usage = kb_api.compact(
        items, model=model, effort=effort, retry_label=label, instructions=instructions
    )
    blobs = [item for item in out if item.get("type") in kb_api.COMPACTION_TYPES]
    if (usage.get("output_tokens") or 0) < thin_threshold:
        out2, secs2, usage2 = kb_api.compact(
            items, model=model, effort=effort,
            retry_label=f"{label} thin-redraw", instructions=instructions,
        )
        blobs2 = [item for item in out2 if item.get("type") in kb_api.COMPACTION_TYPES]
        if (usage2.get("output_tokens") or 0) > (usage.get("output_tokens") or 0):
            blobs, secs, usage = blobs2, secs2, usage2
    if not blobs:
        raise RuntimeError(f"no encrypted compaction item returned for {label}")
    return blobs, secs, usage


def new_state(repo):
    return {"format": "repo-related-packs-v2", "repo": str(repo), "blobs": [],
            "base_items": [], "fed": [], "skipped": [], "rounds": [],
            "prelude_blob_ids": [], "blob_output_tokens": {}}


def load_repo_state(name, repo):
    path = kb_api.state_path(name)
    if not os.path.exists(path):
        return new_state(repo)
    state = json.load(open(path))
    if state.get("format") != "repo-related-packs-v2":
        raise SystemExit(f"KB[{name}] has incompatible format {state.get('format')!r}; use a new --name")
    if Path(state["repo"]).resolve() != repo.resolve():
        raise SystemExit(f"KB[{name}] belongs to another repo: {state['repo']}")
    return state


def save_repo_state(name, state):
    state["base_items"] = state["blobs"]
    state["stage1_blob_count"] = len([
        blob for blob in state["blobs"]
        if blob.get("type") in kb_api.COMPACTION_TYPES
    ])
    measured_tokens = kb_api.current_blob_output_tokens(state)
    if measured_tokens is not None:
        state["stage1_blob_output_tokens"] = measured_tokens
    kb_api.save_state(name, state)


def checkpoint_pack(
    name, state, item, result, order_by_pack, completed, total,
):
    pack_id, label, paths, _source, estimated, is_prelude = item
    blobs, secs, usage = result
    state["blobs"].extend(blobs)
    blob_pack_ids = state.setdefault("blob_pack_ids", {})
    for blob in blobs:
        if blob.get("id"):
            blob_pack_ids[blob["id"]] = pack_id
    # Old v2 state did not record a blob-to-pack mapping. Such blobs are the
    # already-checkpointed canonical prefix, so keep them first. New results
    # can arrive in any order but are materialized in the original plan order.
    indexed = list(enumerate(state["blobs"]))
    indexed.sort(key=lambda pair: (
        -1 if pair[1].get("id") not in blob_pack_ids else
        order_by_pack[blob_pack_ids[pair[1]["id"]]],
        pair[0],
    ))
    state["blobs"] = [blob for _index, blob in indexed]
    kb_api.record_blob_output_tokens(state, blobs, usage)
    if is_prelude:
        state.setdefault("prelude_blob_ids", []).extend(
            blob.get("id", "") for blob in blobs if blob.get("id")
        )
    state["fed"].append(pack_id)
    state["rounds"].append({
        "n": len(state["rounds"]) + 1, "source": label, "source_id": pack_id,
        "plan_order": order_by_pack[pack_id],
        "files": [p.as_posix() for p in paths],
        "estimated_input_tokens": estimated,
        "secs": secs, "usage": usage, "out": kb_api.summarize_items(blobs),
    })
    save_repo_state(name, state)
    print(f"[{completed}/{total}] {label} files={len(paths)} est={estimated} "
          f"-> {len(blobs)} blob out={usage.get('output_tokens')}tok {secs}s",
          flush=True)


def compact_pending(
    name, state, work, order_by_pack, workers, thin_threshold, model, effort,
    instructions=kb_api.CHARTER,
):
    """Compact concurrently while checkpointing every completed pack.

    A slow or ultimately failed early pack must not hide and discard successful
    later futures. Failures are reported only after every other in-flight pack
    has had a chance to finish and persist its result.
    """
    def run_one(item):
        _pack_id, label, _paths, source, _estimated, _is_prelude = item
        return compact_one(label, source, thin_threshold, model, effort, instructions=instructions)

    failures = []
    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(run_one, item): item for item in work}
        for future in as_completed(futures):
            item = futures[future]
            label = item[1]
            try:
                result = future.result()
            except Exception as error:
                failures.append((label, error))
                print(f"failed {label}: {error}", file=sys.stderr, flush=True)
                continue
            completed += 1
            checkpoint_pack(
                name, state, item, result, order_by_pack, completed, len(work),
            )
    if failures:
        labels = ", ".join(label for label, _error in failures)
        raise RuntimeError(f"{len(failures)} pack(s) failed: {labels}") from failures[0][1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--repo-map", required=True)
    parser.add_argument("--reading-instructions-file", help="UTF-8 file replacing each pack's reading instructions")
    parser.add_argument("--instructions-file", help="UTF-8 file replacing compaction API instructions")
    parser.add_argument(
        "--prelude-file", action="append", default=[],
        help="compress this non-repository context first; repeatable",
    )
    parser.add_argument("--name", required=True)
    parser.add_argument("--reuse-manifest", help="previous pack-to-blob mapping")
    parser.add_argument("--budget-tokens", type=int, default=150_000)
    parser.add_argument("--max-file-bytes", type=int, default=600_000)
    parser.add_argument("--thin-threshold", type=int, default=2_000)
    parser.add_argument("--model", default=kb_api.DEFAULT_MODEL)
    parser.add_argument("--effort", choices=("none", "minimal", "low", "medium", "high", "xhigh"),
                        default=kb_api.DEFAULT_EFFORT)
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, help="process at most N pending packs")
    args = parser.parse_args()

    repo = Path(args.repo).expanduser().resolve()
    repo_map_path = Path(args.repo_map).expanduser().resolve()
    try:
        git(repo, "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        raise SystemExit(f"not a git repository: {repo}")
    if not repo_map_path.is_file():
        raise SystemExit(f"repo_map not found: {repo_map_path}")
    repo_map = repo_map_path.read_text(errors="replace")
    reading_instructions = (Path(args.reading_instructions_file).expanduser().read_text(encoding="utf-8")
                            if args.reading_instructions_file is not None else READ_INSTRUCTION)
    instructions = (Path(args.instructions_file).expanduser().read_text(encoding="utf-8")
                    if args.instructions_file is not None else kb_api.CHARTER)
    prelude_files = [Path(path).expanduser().resolve() for path in args.prelude_file]
    for path in prelude_files:
        if not path.is_file():
            raise SystemExit(f"prelude not found: {path}")

    candidates = repomix_candidates(repo)
    included, skipped, contents = [], [], {}
    for rel in tracked_files(repo):
        path = repo / rel.as_posix()
        if not path.is_file():
            continue
        if rel not in candidates:
            skipped.append({"path": rel.as_posix(), "reason": "repomix filter"})
            continue
        data = path.read_bytes()
        reason = skip_reason(rel, data, args.max_file_bytes)
        if reason:
            skipped.append({"path": rel.as_posix(), "reason": reason})
        else:
            included.append(rel)
            contents[rel] = data.decode("utf-8")

    prefix_tokens = kb_api.est_tokens(blob_source(repo_map, [], {}, reading_instructions))
    # File manifests, separators, and JSON message wrapping add a small amount that is not
    # represented by the sum of individual file blocks. Keep a fixed safety margin.
    capacity = args.budget_tokens - prefix_tokens - 2_000
    if capacity <= 0:
        raise SystemExit(f"repo_map/instructions consume {prefix_tokens} tokens, exceeding budget")
    block_tokens = {p: kb_api.est_tokens(file_block(p.as_posix(), contents[p])) for p in included}
    units = build_related_units(included, block_tokens, capacity)
    reusable = json.loads(Path(args.reuse_manifest).read_text()) if args.reuse_manifest else None
    old_packs = []
    changed = set()
    if reusable and reusable.get("reading_sha256") == digest(reading_instructions.encode()) \
            and reusable.get("instructions_sha256") == digest(instructions.encode()) and not prelude_files:
        changed_raw = subprocess.check_output([
            "git", "-C", str(repo), "diff", "--name-only", "-z",
            reusable["commit"], "HEAD", "--",
        ])
        changed = {path.decode("utf-8", "surrogateescape")
                   for path in changed_raw.split(b"\0") if path}
        old_packs = reusable["packs"]
    included_set = set(included)
    reserved = set()
    packs = []
    reuse_by_files = {}
    for old in old_packs:
        old_paths = [PurePosixPath(path) for path in old["files"]]
        paths = [path for path in old_paths if path in included_set and path not in reserved]
        reserved.update(paths)
        if not paths:
            continue
        if sum(block_tokens[path] for path in paths) > capacity:
            packs.extend(pack_units(build_related_units(paths, block_tokens, capacity), block_tokens, capacity))
            continue
        packs.append(paths)
        if paths == old_paths and not any(path.as_posix() in changed for path in paths):
            reuse_by_files[tuple(paths)] = old["blobs"]
    new_paths = [path for path in included if path not in reserved]
    packs.extend(pack_units(build_related_units(new_paths, block_tokens, capacity), block_tokens, capacity))

    state = load_repo_state(args.name, repo)
    state["commit"] = git(repo, "rev-parse", "HEAD")
    state["repo_map"] = str(repo_map_path)
    state["repo_map_sha256"] = digest(repo_map.encode())
    state["budget_tokens"] = args.budget_tokens
    # Count each included tracked text file exactly once. Do not include the repo_map or
    # reading instructions, which are deliberately repeated in every stage-one input.
    state["source_total_tokens"] = sum(block_tokens.values())
    state["model"] = args.model
    state["effort"] = args.effort
    state["reading_instructions"] = reading_instructions
    state["instructions"] = instructions
    state["skipped"] = skipped
    fed = set(state["fed"])
    pending = []
    planned_pack_ids = []
    plan = []
    reused = 0
    prelude_ids = []
    for index, path in enumerate(prelude_files, 1):
        label_name = f"{index:02d}-{path.name}"
        source = prelude_source(repo_map, label_name, path.read_text(errors="replace"), reading_instructions)
        pack_id = digest(source.encode())
        planned_pack_ids.append(pack_id)
        prelude_ids.append(pack_id)
        label = f"prelude-{index:02d}:{path.name}"
        estimated = kb_api.est_tokens(source)
        if estimated > args.budget_tokens:
            raise SystemExit(
                f"prelude {path} consumes {estimated} tokens, exceeding budget {args.budget_tokens}"
            )
        plan.append({
            "label": label, "files": 1, "estimated_tokens": estimated,
            "first": str(path), "last": str(path), "kind": "prelude",
        })
        if pack_id not in fed:
            pending.append((pack_id, label, [PurePosixPath(label_name)], source, estimated, True))

    missing_preludes = [pack_id for pack_id in prelude_ids if pack_id not in fed]
    if missing_preludes and state["blobs"]:
        raise SystemExit(
            "new --prelude-file cannot be inserted before existing blobs; use a fresh --name/state"
        )
    state["prelude_files"] = [str(path) for path in prelude_files]
    for index, paths in enumerate(packs, 1):
        source = blob_source(repo_map, paths, contents, reading_instructions)
        pack_id = digest(source.encode())
        planned_pack_ids.append(pack_id)
        label = f"pack-{index:02d}:{common_directory(paths)}"
        estimated = kb_api.est_tokens(source)
        plan.append({"label": label, "files": len(paths), "estimated_tokens": estimated,
                     "first": paths[0].as_posix(), "last": paths[-1].as_posix(),
                     "reused": tuple(paths) in reuse_by_files})
        if pack_id not in fed:
            if tuple(paths) in reuse_by_files and not args.dry_run:
                blobs = reuse_by_files[tuple(paths)]
                state["blobs"].extend(blobs)
                for blob in blobs:
                    state.setdefault("blob_pack_ids", {})[blob["id"]] = pack_id
                state["fed"].append(pack_id)
                state["rounds"].append({"n": len(state["rounds"]) + 1,
                                        "source": label, "source_id": pack_id,
                                        "plan_order": index - 1,
                                        "files": [path.as_posix() for path in paths],
                                        "estimated_input_tokens": estimated,
                                        "reused": True})
                fed.add(pack_id)
                reused += 1
            else:
                pending.append((pack_id, label, paths, source, estimated, False))

    if reused:
        order = {pack_id: index for index, pack_id in enumerate(planned_pack_ids)}
        state["blobs"].sort(key=lambda blob: order[state["blob_pack_ids"][blob["id"]]])
        state["reused_packs"] = state.get("reused_packs", 0) + reused
        save_repo_state(args.name, state)

    print(json.dumps({
        "repo": str(repo), "commit": state["commit"], "repo_map": str(repo_map_path),
        "repo_map_tokens": kb_api.est_tokens(repo_map), "fixed_prefix_tokens": prefix_tokens,
        "model": args.model, "effort": args.effort,
        "included": len(included), "skipped": len(skipped), "units": len(units),
        "preludes": len(prelude_files), "packs": len(packs),
        "pending": len(pending), "reused": reused, "plan": plan,
    }, ensure_ascii=False, indent=2))
    if args.dry_run:
        return

    work = pending[:args.limit] if args.limit is not None else pending

    order_by_pack = {
        pack_id: index for index, pack_id in enumerate(planned_pack_ids)
    }
    # Prelude blobs must be durable before source packs. Besides preserving the
    # session contract, this prevents a failed prelude from leaving
    # checkpointed source blobs that cannot later be inserted behind it.
    prelude_work = [item for item in work if item[5]]
    source_work = [item for item in work if not item[5]]
    if prelude_work:
        compact_pending(
            args.name, state, prelude_work, order_by_pack, args.workers,
            args.thin_threshold, args.model, args.effort, instructions=instructions,
        )
    if source_work:
        compact_pending(
            args.name, state, source_work, order_by_pack, args.workers,
            args.thin_threshold, args.model, args.effort, instructions=instructions,
        )

    current = Path(kb_api.STATE_ROOT) / "kb-current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(args.name + "\n")
    print(f"complete: blobs={len(state['blobs'])}; kb-current={args.name}")


if __name__ == "__main__":
    main()
