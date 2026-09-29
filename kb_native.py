"""Attach KB context to a native Codex invocation without consuming its arguments."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

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


PROXY_KEY = "kb-proxy"


def _inherited_overrides():
    # Validate the inherited contract before using it.
    kb_codex.config_flags()
    return json.loads(os.environ.get("KB_CODEX_CONFIG_OVERRIDES", "[]"))


def pool_upstream(origin):
    """Return (inherited provider or None, upstream base URL) for kb's proxy.

    pool-rr passes its provider through KB_CODEX_CONFIG_OVERRIDES; its base_url
    is the round-robin route. Otherwise the pool's fill-first Codex route is used.
    """
    provider, tables = None, {}
    for value in _inherited_overrides():
        setting = tomllib.loads(value)
        if "model_provider" in setting:
            provider = setting["model_provider"]
        for name, table in (setting.get("model_providers") or {}).items():
            if isinstance(table, dict):
                tables.setdefault(name, {}).update(table)
    if provider is None:
        return None, origin + "/backend-api/codex"
    base = tables.get(provider, {}).get("base_url")
    if not base:
        raise ValueError(f"KB_CODEX_CONFIG_OVERRIDES の model_providers.{provider}.base_url がありません")
    return provider, base


def proxy_overrides(proxy_url, provider=None):
    """Config overrides (after the inherited ones) that point Codex at kb's proxy.

    The child only holds a dummy key; the proxy adds the pool key upstream.
    """
    if provider is not None:
        return [f"model_providers.{provider}.base_url=" + json.dumps(proxy_url),
                f'model_providers.{provider}.env_key="KB_NATIVE_POOL_KEY"',
                f"model_providers.{provider}.supports_websockets=false",
                f"model_providers.{provider}.requires_openai_auth=false"]
    # Built-in openai ignores provider-table overrides. Use an invocation-only
    # provider for the normal fill-first pool route, with the same local model catalog.
    info = {
        "name": "Account Pool KB",
        "base_url": proxy_url,
        "env_key": "KB_NATIVE_POOL_KEY",
        "wire_api": "responses",
        "requires_openai_auth": False,
        "supports_websockets": False,
    }
    table = "{ " + ", ".join(key + " = " + json.dumps(value) for key, value in info.items()) + " }"
    catalog = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
    if not catalog.is_file():
        raise ValueError("Codexのモデル一覧がありません。通常のcodexを一度起動してください")
    return ['model_provider="kb_pool"', "model_providers.kb_pool=" + table,
            "model_catalog_json=" + json.dumps(str(catalog.resolve()))]


def remote_command(native_args, proxy_url, provider=None):
    overrides = [*_inherited_overrides(), *proxy_overrides(proxy_url, provider)]
    env = dict(os.environ, KB_NATIVE_POOL_KEY=PROXY_KEY)
    flags = [part for value in overrides for part in ("-c", value)]
    return with_config(native_args, flags), env


def start_proxy(remote, items, on_request=None):
    """Start kb's Codex proxy in front of the pool. Returns (running proxy, provider)."""
    provider, upstream = pool_upstream(remote.origin)
    running = proxy.start(proxy.codex_injector(items), upstream, on_request=on_request,
                          set_headers={"Authorization": f"Bearer {remote.key}"})
    return running, provider


def run_remote(native_args, remote, items, workspace, *, on_request=None, runner=None):
    running, provider = start_proxy(remote, items, on_request)
    with running:
        command, env = remote_command(native_args, running.url, provider)
        count = len(items) if isinstance(items, list) else "lazy"
        print(f"Remote KB: proxy 127.0.0.1:{running.port} / {count} items", file=sys.stderr, flush=True)
        return proxy.run_client(command, env, workspace, runner)


def run(args, config, snapshot, workspace, context, *, developer_text=None):
    if args.remote:
        remote = RemoteKB(config, snapshot, developer_text=developer_text)
        if context:
            remote.items.append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": context}]})
        # kb's proxy inserts the items into each inference; nothing is bound in the pool.
        return run_remote(args.native_args, remote, remote.items, workspace)
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
    """`kb codex NAME --remote`: seed a thread, then resume it through kb's proxy."""
    remote = RemoteKB(config, snapshot, developer_text=developer_text)
    # The seeded thread persists the guidance item (and update context) locally,
    # so the proxy inserts the rest; nothing is doubled.
    running, provider = start_proxy(remote, remote.items[1:])
    with running:
        added = proxy_overrides(running.url, provider)
        os.environ["KB_NATIVE_POOL_KEY"] = PROXY_KEY
        seeded = _Seeded(args)
        session_id = kb_codex.start_session(snapshot, workspace, args.name, context,
                                            full_access=not args.no_yolo, prompt=args.prompt,
                                            remote=seeded, overrides=added, developer_text=developer_text)
        print(f"session: {session_id}", flush=True)
        print(f"Remote KB: proxy 127.0.0.1:{running.port} / {len(remote.items) - 1} items", flush=True)
        if args.session_only or args.app:
            if args.app:
                subprocess.run(["open", f"codex://threads/{session_id}"], check=True)
            print("このプロセスの終了後はKBを挿入しません。KB付きで続けるには: "
                  f"kb --remote codex resume {session_id}", file=sys.stderr, flush=True)
            return session_id
        command = ["codex", "resume", *kb_codex.config_flags(added)]
        if not args.no_yolo:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        command += ["-C", str(workspace), session_id]
        return proxy.run_client(command, dict(os.environ), workspace, runner)


class _Seeded:
    """Stands in for the pool binding in kb_codex.start_session."""

    def __init__(self, args):
        self.args = args

    def bind(self, session_id, *, include_guidance=True):
        return session_id
