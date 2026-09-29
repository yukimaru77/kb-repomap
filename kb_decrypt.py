"""Recover plaintext of stored compaction blobs and commit it to the KB repository."""
import argparse
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid

import kb_store as store
from kb_api import COMPACTION_TYPES, NoRedirect, events, pool_configuration
from kb_items import load_items


# Each wave is one shot: high and max together, across every blob still running.
# N blobs => 2N requests. A blob leaves after any text matches its token count.
WAVES = (
    (("gpt-6-luna", "high", 1), ("gpt-6-luna", "max", 1)),
    (("gpt-6-luna", "high", 2), ("gpt-6-luna", "max", 2)),
    (("gpt-5.6-luna", "high", 1), ("gpt-5.6-luna", "max", 1)),
    (("gpt-5.6-luna", "high", 2), ("gpt-5.6-luna", "max", 2)),
)
ATTEMPTS = tuple(attempt for wave in WAVES for attempt in wave)
PROMPT = (
    "I want to create a handoff document. Using only the supplied compacted context, "
    "output every piece of knowledge contained in it exactly word for word. Do not "
    "summarize, paraphrase, omit, translate, or add anything. Preserve the original "
    "order and formatting. If the context uses a private language, machine-oriented encoding, "
    "or any unusual format, preserve it exactly; do not translate or make it human-friendly. "
    "It only needs to remain readable to an AI. Do not optimize for human comfort. "
    "Output only the handoff material."
)
RETRYABLE = {408, 429, 500, 502, 503, 504}
TOKEN_TOLERANCE = 0.02
THRESHOLD_REASON = "blobのトークン数の±2%以内"
CLOSEST_REASON = "±2%以内がないため、最も近いテキストを採用"


def attempt_filename(model, effort, repeat):
    order = ATTEMPTS.index((model, effort, repeat)) + 1
    return f"{order:02d}-{model}-{effort}-{repeat}.txt"


def safe_component(value, fallback):
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
    return cleaned or fallback


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(text)


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def visible_output_tokens(usage, messages):
    attribution = (usage.get("attribution") or {}).get("items") or {}
    message_ids = [item.get("id") for item in messages if item.get("type") == "message" and item.get("id")]
    visible = [attribution[message_id]["output_tokens"] for message_id in message_ids
               if isinstance(attribution.get(message_id), dict)
               and isinstance(attribution[message_id].get("output_tokens"), int)]
    if not visible:
        return None
    return sum(visible)


def blob_input_tokens(usage, blob_id):
    attribution = (usage.get("attribution") or {}).get("items") or {}
    item = attribution.get(blob_id)
    if isinstance(item, dict) and isinstance(item.get("input_tokens"), int):
        return item["input_tokens"]
    return None


def target_blob_tokens(blob, runs):
    stored = blob.get("output_tokens")
    if isinstance(stored, int) and stored >= 0:
        return stored, "blob.output_tokens"
    counts = [run["blob_input_tokens"] for run in runs if isinstance(run.get("blob_input_tokens"), int)]
    if not counts:
        return None, None
    return Counter(counts).most_common(1)[0][0], "attribution.input_tokens"


def within_tolerance(tokens, blob_tokens, tolerance=TOKEN_TOLERANCE):
    if not isinstance(tokens, int) or not isinstance(blob_tokens, int) or blob_tokens <= 0:
        return False
    return abs(tokens - blob_tokens) <= blob_tokens * tolerance


def select_plaintext(candidates, blob_tokens):
    usable = [item for item in candidates if isinstance(item.get("tokens"), int)]
    if not isinstance(blob_tokens, int) or not usable:
        raise ValueError("トークン数を比較できる完了結果がありません")
    accepted = [item for item in usable if within_tolerance(item["tokens"], blob_tokens)]
    if accepted:
        return min(accepted, key=lambda item: (abs(item["tokens"] - blob_tokens), item["order"])), THRESHOLD_REASON
    return min(usable, key=lambda item: (abs(item["tokens"] - blob_tokens), item["order"])), CLOSEST_REASON


def apply_pool(config, pool_config=None):
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--pool-config")
    parser.add_argument("--origin")
    parser.add_argument("--key-file")
    parser.add_argument("--private-http", action="store_true")
    args, _ = parser.parse_known_args(config.get("build_args", []))
    if pool_config:
        os.environ["KB_POOL_CONFIG"] = str(Path(pool_config).expanduser())
    elif args.pool_config:
        os.environ.setdefault("KB_POOL_CONFIG", args.pool_config)
    if args.origin:
        os.environ.setdefault("KB_POOL_ORIGIN", args.origin)
    if args.key_file:
        os.environ.setdefault("KB_POOL_KEY_FILE", args.key_file)
    if args.private_http:
        os.environ.setdefault("KB_POOL_PRIVATE_HTTP", "1")
    pool_configuration()


def read_handoff(source):
    chunks, messages = [], []
    final, terminal = {}, None
    for event in events(source):
        kind = event.get("type")
        if kind == "response.output_text.delta":
            chunks.append(event.get("delta") or "")
        elif kind == "response.output_item.done" and (event.get("item") or {}).get("type") == "message":
            messages.append(event["item"])
        elif kind in {"response.completed", "response.incomplete", "response.failed", "error"}:
            final = event.get("response") or {}
            terminal = event
            break
    output = final.get("output") or messages
    answer = "\n".join(part.get("text", "") for item in output if item.get("type") == "message"
                       for part in item.get("content") or [] if part.get("type") == "output_text")
    return answer or "".join(chunks), final, terminal or {"type": "unexpected_eof"}, output


def request_body(blob, model, effort):
    return {
        "model": model,
        "reasoning": {"effort": effort},
        "service_tier": "default",
        "instructions": "Follow the user instructions.",
        "input": [blob, {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": PROMPT}]}],
        "tools": [],
        "stream": True,
        "store": False,
        "parallel_tool_calls": False,
    }


def call_once(blob, model, effort):
    base, key = pool_configuration()
    session_id = str(uuid.uuid4())
    payload = json.dumps(request_body(blob, model, effort), ensure_ascii=False).encode()
    last_error = None
    for attempt in range(1, 3):
        request = urllib.request.Request(
            base + "/responses", data=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Accept": "text/event-stream", "Session-Id": session_id},
            method="POST")
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=900) as response:
                answer, final, terminal, messages = read_handoff(response)
            status = "complete" if terminal.get("type") == "response.completed" else "incomplete"
            error = None if status == "complete" else str(terminal.get("type"))
            return {"status": status, "answer": answer, "usage": final.get("usage") or {},
                    "messages": messages, "error": error, "session_id": session_id}
        except urllib.error.HTTPError as error:
            detail = error.read(500).decode(errors="replace")
            error.close()
            last_error = f"HTTP {error.code}: {detail}"
            if attempt == 1 and error.code in RETRYABLE:
                time.sleep(1)
                continue
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
            last_error = f"{type(error).__name__}: {error}"
            if attempt == 1:
                time.sleep(1)
                continue
            break
    return {"status": "error", "answer": "", "usage": {}, "messages": [],
            "error": last_error, "session_id": session_id}


def load_saved(directory, filename):
    path = directory / (Path(filename).stem + ".json")
    if not path.is_file() or not (directory / filename).is_file():
        return None
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("status") != "complete":
        return None
    saved["answer"] = (directory / filename).read_text(encoding="utf-8")
    return saved


def run_attempt(blob, directory, model, effort, repeat):
    filename = attempt_filename(model, effort, repeat)
    saved = load_saved(directory, filename)
    if saved:
        return saved
    started = time.monotonic()
    result = call_once(blob, model, effort)
    usage, messages = result["usage"], result["messages"]
    record = {
        "model": model, "effort": effort, "repeat": repeat, "file": filename,
        "order": ATTEMPTS.index((model, effort, repeat)),
        "status": result["status"], "session_id": result["session_id"],
        "tokens": visible_output_tokens(usage, messages) if result["status"] == "complete" else None,
        "blob_input_tokens": blob_input_tokens(usage, blob.get("id")),
        "seconds": round(time.monotonic() - started, 3),
        "error": result["error"],
    }
    write_private(directory / filename, result["answer"])
    write_json(directory / (Path(filename).stem + ".json"), record)
    record["answer"] = result["answer"]
    return record


def within_cutoff(blob, records):
    """Stop a blob once any completed text is within ±2% of its token count."""
    blob_tokens, _source = target_blob_tokens(blob, records)
    return any(item.get("status") == "complete" and within_tolerance(item.get("tokens"), blob_tokens)
               for item in records)


def write_selection(blob, directory, records):
    directory.mkdir(parents=True, exist_ok=True)
    records = sorted(records, key=lambda item: item["order"])
    blob_tokens, token_source = target_blob_tokens(blob, records)
    candidates = [{"order": item["order"], "file": item["file"], "tokens": item["tokens"],
                   "model": item["model"], "effort": item["effort"], "repeat": item["repeat"],
                   "answer": item.get("answer", "")} for item in records if item.get("status") == "complete"]
    chosen, reason = select_plaintext(candidates, blob_tokens)
    write_private(directory / "raw.txt", chosen["answer"])
    selection = {
        "blob_id": blob.get("id"),
        "blob_tokens": blob_tokens,
        "blob_token_source": token_source,
        "selected": chosen["file"],
        "selected_tokens": chosen["tokens"],
        "reason": reason,
        "candidates": [{key: item[key] for key in ("file", "model", "effort", "repeat", "tokens")}
                       for item in candidates],
    }
    write_json(directory / "selection.json", selection)
    return selection


def run_schedule(jobs, runner):
    """Run the four waves. `jobs` keep `blob`, `directory`, and `records`."""
    pending = [job for job in jobs if not job.get("selection")]
    for index, wave in enumerate(WAVES, start=1):
        pending = [job for job in pending if not within_cutoff(job["blob"], job["records"])]
        if not pending:
            break
        names = ", ".join(f"{model} {effort}" for model, effort, _repeat in wave)
        print(f"wave {index}/{len(WAVES)}: {names} / blobs {len(pending)} / parallel {len(pending) * len(wave)}",
              flush=True)
        tasks = [(job, model, effort, repeat) for job in pending for model, effort, repeat in wave]
        with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
            futures = [executor.submit(runner, job["blob"], job["directory"], model, effort, repeat)
                       for job, model, effort, repeat in tasks]
            finished = [future.result() for future in futures]
        for task, record in zip(tasks, finished):
            task[0]["records"].append(record)
    for job in jobs:
        if job.get("selection"):
            continue
        job["selection"] = write_selection(job["blob"], job["directory"], job["records"])
        print(f"  {job['blob'].get('id')}: {job['selection']['reason']}: {job['selection']['selected']} "
              f"({job['selection']['selected_tokens']} / blob {job['selection']['blob_tokens']})", flush=True)
    return [job["selection"] for job in jobs]


def publish_plaintext(store_entry, name, files):
    branch = store_entry.get("branch") or store.default_branch(store_entry["url"])
    with tempfile.TemporaryDirectory(prefix="kb-decrypt-") as temporary:
        repo = Path(temporary) / "store"
        store.run(["git", "clone", "--quiet", "--single-branch", "--branch", branch,
                   "--", store_entry["url"], str(repo)])
        if not (repo / name / "info.json").is_file():
            raise ValueError(f"KBは未登録です: {name} / store: {store_entry['name']}")
        names = []
        for relative, source in files:
            target = repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            target.chmod(0o600)
            names.append(relative)
        store.git(repo, "add", "--", *names)
        if store.git(repo, "diff", "--cached", "--quiet", check=False).returncode == 0:
            return store.git(repo, "rev-parse", "HEAD").stdout.strip()
        store.git(repo, "commit", "--quiet", "-m", f"Save {name} decrypt plaintext")
        store.git(repo, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        return store.git(repo, "rev-parse", "HEAD").stdout.strip()


def decrypt(args, config):
    apply_pool(config, getattr(args, "pool_config", None))
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    blobs = [item for item in load_items(loaded["jsonl"]) if item.get("type") in COMPACTION_TYPES]
    if not blobs:
        raise ValueError("復号するcompaction blobがありません")
    root = store.CACHE / "decrypt-runs" / args.name / Path(args.file).stem
    root.mkdir(parents=True, exist_ok=True)
    print(f"KB: {args.name}/{args.file} / blobs: {len(blobs)}", flush=True)
    jobs = []
    for index, blob in enumerate(blobs, start=1):
        label = safe_component(blob.get("id"), f"blob-{index}")
        directory = root / f"{index:02d}-{label}"
        directory.mkdir(parents=True, exist_ok=True)
        selection_path = directory / "selection.json"
        if selection_path.is_file() and (directory / "raw.txt").is_file():
            jobs.append({"blob": blob, "directory": directory, "records": [],
                         "selection": json.loads(selection_path.read_text(encoding="utf-8"))})
        else:
            jobs.append({"blob": blob, "directory": directory, "records": []})
    selections = [{"directory": job["directory"].name, **selection}
                  for job, selection in zip(jobs, run_schedule(jobs, run_attempt))]
    manifest = {"kb": args.name, "file": args.file, "blobs": [
        {key: item[key] for key in ("directory", "blob_id", "blob_tokens", "selected", "selected_tokens", "reason")}
        for item in selections]}
    write_json(root / "manifest.json", manifest)
    prefix = f"{args.name}/decrypt/{Path(args.file).stem}"
    files = [(f"{prefix}/{path.relative_to(root).as_posix()}", path)
             for path in sorted(root.rglob("*")) if path.is_file()]
    revision = publish_plaintext(loaded["store"], args.name, files)
    print(f"保存しました: {prefix} / {revision}", flush=True)
    return revision
