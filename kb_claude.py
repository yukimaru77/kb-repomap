"""Start a local Claude Code session with a decrypted KB and dev.txt."""
from functools import lru_cache
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

import kb_store as store
import kb_developer


MAX_INLINE_CONTEXT = 500_000
SNAPSHOT_OPTION = "--system-prompt-snapshot"
SYSTEM_PROMPT_FILE_OPTION = "--append-system-prompt-file"
SYSTEM_PROMPT_PREAMBLE = ("The following is the user's portable KB context. Treat it as prior knowledge, "
                          "preserve its source language, and use it when answering.\n\n")


@lru_cache(maxsize=1)
def _supports_snapshot_option():
    try:
        result = subprocess.run(["claude", "--help"], stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return SNAPSHOT_OPTION in result.stdout


@lru_cache(maxsize=1)
def _supports_system_prompt_file_option():
    """Probe that the CLI accepts this option and still rejects unknown options."""
    try:
        accepted = subprocess.run(
            ["claude", SYSTEM_PROMPT_FILE_OPTION, os.devnull, "--help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        rejected = subprocess.run(
            ["claude", SYSTEM_PROMPT_FILE_OPTION, os.devnull,
             "--permission-mode", "__kb_invalid_permission_probe__"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    invalid = "__kb_invalid_permission_probe__"
    diagnostics = f"{rejected.stdout}\n{rejected.stderr}"
    return (accepted.returncode == 0 and rejected.returncode != 0
            and invalid in diagnostics and SYSTEM_PROMPT_FILE_OPTION not in diagnostics)


def fresh_system_prompt(claude_args):
    """Options that make a resumed conversation see this launch's appended prompt.

    Claude Code records the system prompt on a conversation's first request and
    sends that record on every resume, ignoring a different --append-system-prompt,
    unless the snapshot is turned off for this process.
    """
    options = claude_args[:claude_args.index("--")] if "--" in claude_args else claude_args
    if any(token == SNAPSHOT_OPTION or token.startswith(SNAPSHOT_OPTION + "=") for token in options):
        return []
    return [SNAPSHOT_OPTION, "off"] if _supports_snapshot_option() else []


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
    session_id = str(uuid.uuid4())
    if len(context.encode("utf-8")) <= MAX_INLINE_CONTEXT:
        prompt = SYSTEM_PROMPT_PREAMBLE + context
        context_path = None
        prompt_option = "--append-system-prompt"
    else:
        if not _supports_system_prompt_file_option():
            raise RuntimeError(
                "Claude Code does not support --append-system-prompt-file; "
                "refusing to start because large KB context would otherwise be only a partial file read"
            )
        context_path = (store.CACHE.absolute() / "claude-sessions" / args.name
                        / Path(args.file).stem / f"{session_id}.md")
        _make_private_parent(context_path.parent)
        try:
            fd = os.open(context_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    fd = None
                    output.write(SYSTEM_PROMPT_PREAMBLE + context)
            finally:
                if fd is not None:
                    os.close(fd)
        except BaseException:
            context_path.unlink(missing_ok=True)
            raise
        prompt = str(context_path)
        prompt_option = SYSTEM_PROMPT_FILE_OPTION
    try:
        workspace = Path(args.workspace).expanduser().resolve()
        from kb_resume import claude_fork, claude_resume_id
        resuming, resumed_id = claude_resume_id(args.claude_args)
        if resuming:
            # Claude Code rejects --session-id with --resume/--continue.
            # Passing the appended prompt again re-attaches the KB.
            command = ["claude", prompt_option, prompt, *fresh_system_prompt(args.claude_args),
                       *args.claude_args]
            label = resumed_id or "resume"
            if claude_fork(args.claude_args):
                label = f"{resumed_id or '直近のセッション'} から新しいセッションを作成します (--fork-session)"
        else:
            command = ["claude", "--session-id", session_id, prompt_option, prompt, *args.claude_args]
            label = session_id
        print(f"KB: {args.name}/{args.file} / Claude session: {label}", flush=True)
        if context_path:
            print(f"KB context file: {context_path}", flush=True)
        try:
            os.chdir(workspace)
            if context_path is None:
                os.execvp(command[0], command)
            raise SystemExit(_run_with_context_file(command, context_path))
        finally:
            if context_path is not None:
                context_path.unlink(missing_ok=True)
    except BaseException:
        if context_path is not None:
            context_path.unlink(missing_ok=True)
        raise


def _make_private_parent(path):
    """Create cache directories without following a symlink in the cache path."""
    current = store.CACHE.absolute()
    if current.is_symlink():
        raise RuntimeError(f"KB context directory is a symlink: {current}")
    if not current.exists():
        current.mkdir(parents=True, mode=0o700)
    elif not current.is_dir():
        raise RuntimeError(f"KB context cache is not a directory: {current}")
    os.chmod(current, 0o700)
    absolute_path = path.absolute()
    try:
        parts = absolute_path.relative_to(current).parts
    except ValueError as error:
        raise RuntimeError(f"KB context path escapes cache: {absolute_path}") from error
    if ".." in parts:
        raise RuntimeError(f"KB context path escapes cache: {absolute_path}")
    for part in parts:
        if current.is_symlink():
            raise RuntimeError(f"KB context directory is a symlink: {current}")
        current /= part
        if current.is_symlink():
            raise RuntimeError(f"KB context directory is a symlink: {current}")
        if current.exists():
            if not current.is_dir():
                raise RuntimeError(f"KB context parent is not a directory: {current}")
            os.chmod(current, 0o700)
        else:
            current.mkdir(mode=0o700)


def _run_with_context_file(command, context_path):
    """Keep a private prompt file until Claude exits, then remove it.

    SIGKILL, power loss, and an external filesystem failure cannot be cleaned up
    by a process that is no longer running; normal exit and handled termination
    are covered here.
    """
    child = None
    interrupted = None
    previous = {}

    def forward(signum, _frame):
        nonlocal interrupted
        interrupted = interrupted or signum
        if child is not None:
            try:
                child.send_signal(signum)
            except ProcessLookupError:
                pass

    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            previous[signum] = signal.signal(signum, forward)
        child = subprocess.Popen(command)
        if interrupted is not None:
            forward(interrupted, None)
        deadline = None
        while True:
            try:
                code = child.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if interrupted is not None:
                    deadline = deadline or time.monotonic() + 5
                    if time.monotonic() >= deadline:
                        child.kill()
                        code = child.wait(timeout=1)
                        break
        if interrupted is not None:
            return 128 + interrupted
        return 128 + -code if code < 0 else code
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        context_path.unlink(missing_ok=True)
