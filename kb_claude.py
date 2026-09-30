"""Start a local Claude Code session with a decrypted KB and dev.txt."""
import os
from pathlib import Path
import uuid

import kb_store as store


MAX_INLINE_CONTEXT = 500_000


def _decrypt_files(loaded, name, filename):
    """Return the selected plaintext files from the KB store."""
    repo, revision, _branch = store.sync_store(loaded["store"])
    prefix = f"{name}/decrypt/{Path(filename).stem}"
    listing = store.git(repo, "ls-tree", "-r", "--name-only", revision, prefix).stdout.splitlines()
    files = []
    for relative in listing:
        if not relative.endswith("/raw.txt"):
            continue
        text = store.git(repo, "show", f"{revision}:{relative}").stdout
        files.append((relative, text))
    return files


def build_context(loaded, name, filename):
    """Build the Claude startup context from store-owned text only."""
    parts = []
    files = _decrypt_files(loaded, name, filename)
    if not files:
        raise ValueError(
            f"復号済みKBがありません: {name}/{filename}。先に `kb decrypt {name} --file {filename}` を実行してください"
        )
    for relative, text in files:
        parts.append(f"## Decrypted KB material: {relative}\n\n{text.rstrip()}")
    # dev.txt follows the material, as guidance on how to use it.
    developer = loaded.get("developer_text")
    if developer and developer.strip():
        parts.append("## KB developer notes\n\n" + developer.rstrip())
    return "\n\n".join(parts) + "\n"


def start(args, config):
    if any(token == "--session-id" or token.startswith("--session-id=") for token in args.claude_args):
        raise ValueError("--session-id はkbが作成するため、Claude側では指定しないでください")
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    context = build_context(loaded, args.name, args.file)
    session_id = str(uuid.uuid4())
    if len(context.encode("utf-8")) <= MAX_INLINE_CONTEXT:
        prompt = (
            "The following is the user's portable KB context. Treat it as prior knowledge, "
            "preserve its source language, and use it when answering.\n\n" + context
        )
        context_path = None
    else:
        context_path = store.CACHE / "claude-sessions" / args.name / Path(args.file).stem / f"{session_id}.md"
        context_path.parent.mkdir(parents=True, exist_ok=True)
        context_path.write_text(context, encoding="utf-8")
        context_path.chmod(0o600)
        prompt = (
            "The user's portable KB context is in this UTF-8 file. Read it before answering and "
            f"treat it as prior knowledge: {context_path}"
        )
    workspace = Path(args.workspace).expanduser().resolve()
    command = ["claude", "--session-id", session_id, "--append-system-prompt", prompt, *args.claude_args]
    print(f"KB: {args.name}/{args.file} / Claude session: {session_id}", flush=True)
    if context_path:
        print(f"KB context file: {context_path}", flush=True)
    os.chdir(workspace)
    os.execvp(command[0], command)
