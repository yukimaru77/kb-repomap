"""Attach KB context to a native Codex invocation without consuming its arguments."""
import argparse
import os
import subprocess
import sys

import kb_codex
import kb_remote_proxy as proxy
import kb_store
from kb_remote import RemoteKB


def parse(argv):
    boundary = argv.index("codex", 1)
    parser = argparse.ArgumentParser(prog="kb NAME [KB options] codex [CODEX args]", allow_abbrev=False)
    parser.add_argument("name", type=kb_store.name_value)
    parser.add_argument("--remote", action="store_true", help="kbのローカルプロキシで推論時だけKBを挿入する")
    parser.add_argument("--store")
    parser.add_argument("--file", default="latest.json")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--rebuild", choices=("never", "always"), default="never")
    parser.add_argument("--clean", action="store_true", help="再作成時に前回blobを再利用せず全体を作り直す")
    args = parser.parse_args(argv[:boundary])
    from kb_cli import file_argument
    args.file = file_argument(args.file)
    args.native_args = argv[boundary + 1:]
    return args


def with_config(native_args, flags):
    # Exec's config options belong after its subcommand in current Codex.
    # Do not parse or rebuild the options, prompt, or stdin that follow it.
    if native_args[:1] and native_args[0] in ("exec", "e", "review", "resume", "fork", "app-server"):
        return ["codex", native_args[0], *flags, *native_args[1:]]
    if native_args and native_args[0].startswith("-"):
        from kb_native_local import subcommand_index
        index = subcommand_index(native_args)
        if index is not None:
            return ["codex", *native_args[:index + 1], *flags, *native_args[index + 1:]]
    return ["codex", *flags, *native_args]


def launch_remote(native_args, binder, workspace, *, runner=None):
    """Run Codex --remote in stealth mode: Codex keeps its own provider, login and transport."""
    import kb_stealth
    count = len(binder.cached) if isinstance(binder.cached, list) else "lazy"
    payload = {"client": "codex", "record": binder.record, "items": binder.cached}
    return kb_stealth.launch("codex", ["codex", *native_args], payload, workspace,
                             describe=lambda port: f"Remote KB: proxy 127.0.0.1:{port} / {count} items",
                             runner=runner)


def run(args, config, snapshot, workspace, context, *, developer_text=None):
    if args.remote:
        remote = RemoteKB(config, snapshot, developer_text=developer_text)
        if context:
            remote.items.append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": context}]})
        # kb's stealth proxy inserts the items into each inference.
        # The session id is Codex's, so bind it when the first request shows it.
        from kb_resume import CodexBinder
        binder = CodexBinder(config, _record(args), remote.items)
        return launch_remote(args.native_args, binder, workspace)
    from kb_native_local import command as local_command, seed_overrides
    # Validate the command shape before creating a persisted session.
    local_command(args.native_args, "validation-only")
    session_id = kb_codex.start_session(snapshot, workspace, args.name, context,
                                         full_access=False, run_initial_turn=False,
                                         overrides=seed_overrides(args.native_args),
                                         developer_text=developer_text)
    command = local_command(args.native_args, session_id)
    command = with_config(command[1:], kb_codex.config_flags())
    os.chdir(workspace)
    os.execvp(command[0], command)


def run_legacy_remote(args, config, snapshot, workspace, context, *, developer_text=None,
                      runner=None):
    """`kb codex NAME --remote`: seed a thread, then resume it behind kb's stealth proxy."""
    import kb_stealth
    remote = RemoteKB(config, snapshot, developer_text=developer_text)
    # The seeded thread persists no KB item (only update context or a seed
    # marker), so the proxy inserts all items, dev.txt included; nothing is doubled.
    record = _record(args)
    payload = {"client": "codex", "record": record, "items": remote.items}
    with kb_stealth.stealth_session("codex", payload) as session:
        # The seeding app-server (and its optional first turn) runs behind the same proxy.
        env = session.client_env("codex")
        session_id = kb_codex.start_session(snapshot, workspace, args.name, context,
                                            full_access=not args.no_yolo, prompt=args.prompt,
                                            remote=_Seeded(record), developer_text=developer_text, env=env)
        print(f"session: {session_id}", flush=True)
        print(f"Remote KB: proxy 127.0.0.1:{session.port} / {len(remote.items)} items / stealth", flush=True)
        if args.session_only or args.app:
            if args.app:
                subprocess.run(["open", f"codex://threads/{session_id}"], check=True)
            print("このプロセスの終了後はKBを挿入しません。KB付きで続けるには: "
                  f"kb --remote codex resume {session_id}", file=sys.stderr, flush=True)
            return session_id
        command = ["codex", "resume"]
        if not args.no_yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command += ["-C", str(workspace), session_id]
        return kb_stealth.run_client(session, "codex", command, workspace, runner)


def _record(args):
    return {"name": args.name, "store": getattr(args, "resolved_store", None) or args.store, "file": args.file}


class _Seeded:
    """The `remote` hook of kb_codex.start_session: write kb's binding for the new thread."""

    def __init__(self, record):
        self.record = record

    def bind(self, session_id):
        proxy.write_binding(session_id, "codex", self.record["name"], self.record["store"], self.record["file"],
                            source="thread/start")
        return session_id
