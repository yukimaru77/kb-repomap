"""Start a local Claude Code session with a decrypted KB and dev.txt."""
import json
import os
from pathlib import Path

import kb_store as store
import kb_developer


def _decrypt_files(loaded, name, filename):
    """Return the selected plaintext files from the KB store."""
    repo, revision, _branch = store.sync_store(loaded["store"])
    prefix = f"{name}/decrypt/{Path(filename).stem}"
    listing = store.git(repo, "ls-tree", "-r", "--name-only", revision, prefix).stdout.splitlines()
    present = set(listing)
    files = []
    for relative in listing:
        if not relative.endswith("/raw.txt"):
            continue
        # Prefer the meta-stripped text (conversation/instruction lines removed) when published.
        knowledge = relative[: -len("raw.txt")] + "knowledge.txt"
        chosen = knowledge if knowledge in present else relative
        text = store.git(repo, "show", f"{revision}:{chosen}").stdout
        files.append((chosen, text))
    return files


SCOPE_NOTE = ("Aliases, abbreviations and shorthand defined inside this section apply only within this "
              "section; other sections may reuse the same symbols with different meanings.")


def window_sections(loaded, files):
    """Each plaintext wrapped as a numbered section that bounds the scope of its own aliases.

    Section names come from the KB's domain windows when their count matches the plaintext files."""
    windows = (loaded.get("info") or {}).get("windows") or []
    total = len(files)
    sections = []
    for index, (_path, text) in enumerate(files, 1):
        title = windows[index - 1].get("name") if len(windows) == total else None
        heading = f"## KB section {index}/{total}" + (f": {title}" if title else "")
        sections.append(f"{heading}\n> {SCOPE_NOTE}\n\n{text.strip()}\n\n## End of KB section {index}/{total}")
    return sections


def codex_kb_text(loaded, name, filename):
    """Decrypted plaintext of exactly this KB's blobs for a Codex session, or None.

    None (use the compaction blobs) when no plaintext is published or it was made from other blobs,
    e.g. right after a launch-time rebuild that did not decrypt."""
    from kb_api import COMPACTION_TYPES
    from kb_items import load_items
    blob_ids = [item.get("id") for item in load_items(loaded["jsonl"]) if item.get("type") in COMPACTION_TYPES]
    if not blob_ids:
        return None
    repo, revision, _branch = store.sync_store(loaded["store"])
    prefix = f"{name}/decrypt/{Path(filename).stem}"
    shown = store.git(repo, "show", f"{revision}:{prefix}/manifest.json", check=False)
    try:
        manifest = json.loads(shown.stdout) if shown.returncode == 0 else None
    except ValueError:
        manifest = None
    if not manifest or [blob.get("blob_id") for blob in manifest.get("blobs", [])] != blob_ids:
        return None
    files = _decrypt_files(loaded, name, filename)
    if len(files) != len(blob_ids):
        return None
    return "\n\n".join(window_sections(loaded, files)) + "\n"


def build_context(loaded, name, filename):
    """Build the Claude startup context from store-owned text only."""
    parts = []
    files = _decrypt_files(loaded, name, filename)
    if not files:
        raise ValueError(
            f"復号済みKBがありません: {name}/{filename}。先に `kb decrypt {name} --file {filename}` を実行してください"
        )
    parts.extend(window_sections(loaded, files))
    # dev.txt follows the material, as guidance on how to use it.
    developer = loaded.get("developer_text")
    if developer and developer.strip():
        parts.append("## KB developer notes\n\n" + developer.rstrip())
    return "\n\n".join(parts) + "\n"


def start(args, config):
    if any(token == "--session-id" or token.startswith("--session-id=") for token in args.claude_args):
        raise ValueError("--session-id はkbが作成するため、Claude側では指定しないでください")
    additions = kb_developer.read_additions(args)
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    loaded = {**loaded, "developer_text": kb_developer.append_text(loaded.get("developer_text"), additions)}
    context = build_context(loaded, args.name, args.file)
    workspace = Path(args.workspace).expanduser().resolve()
    from kb_resume import claude_fork, claude_latest_session, claude_resume_id
    from kb_claude_history import require_existing_seed, seed_new_session
    child_env = dict(os.environ)
    if child_env.get("CLAUDE_CONFIG_DIR"):
        # Resolve before chdir: Claude and the seed writer must use the same
        # directory even when the caller supplies a relative config path.
        child_env["CLAUDE_CONFIG_DIR"] = str(Path(child_env["CLAUDE_CONFIG_DIR"]).expanduser().resolve())
    options = args.claude_args[:args.claude_args.index("--")] if "--" in args.claude_args else args.claude_args
    if "--no-session-persistence" in options:
        raise ValueError("Local Claude KB history requires a persistent session; "
                         "--no-session-persistence cannot be used safely")
    resuming, resumed_id = claude_resume_id(args.claude_args)
    seed_path = None
    if resuming:
        native_args = list(args.claude_args)
        if not resumed_id and any(flag in options for flag in ("--continue", "-c")):
            # Resolve once, validate that exact transcript, then resume it
            # explicitly so a newly modified session cannot change selection.
            resumed_id = claude_latest_session(workspace, env=child_env)
            if not resumed_id:
                raise ValueError("No Claude session is available to continue")
            for index, token in enumerate(native_args):
                if token == "--":
                    break
                if token in ("--continue", "-c"):
                    native_args[index:index + 1] = ["--resume", resumed_id]
                    break
        if not resumed_id:
            raise ValueError("Use --resume with an explicit KB session UUID; named sessions "
                             "cannot be verified safely without modifying an existing transcript")
        require_existing_seed(workspace, resumed_id, context, env=child_env)
        command = ["claude", *native_args]
        label = resumed_id
        if claude_fork(args.claude_args):
            label = f"{resumed_id or '直近のセッション'} から新しいセッションを作成します (--fork-session)"
    else:
        session_id, seed_path = seed_new_session(workspace, context, env=child_env)
        command = ["claude", "--resume", session_id, *args.claude_args]
        label = session_id
    print(f"KB: {args.name}/{args.file} / Claude session: {label}", flush=True)
    try:
        os.chdir(workspace)
        os.execvpe(command[0], command, child_env)
    except (OSError, ValueError):
        if seed_path is not None:
            seed_path.unlink(missing_ok=True)
        raise
