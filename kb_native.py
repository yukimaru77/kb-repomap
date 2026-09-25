"""Attach KB context to a native Codex invocation without consuming its arguments."""
import argparse
import json
import os
from pathlib import Path
import uuid
import tomllib

import kb_codex
import kb_store
from kb_remote import RemoteKB


def parse(argv):
    boundary = argv.index("codex", 1)
    parser = argparse.ArgumentParser(prog="kb NAME [KB options] codex [CODEX args]", allow_abbrev=False)
    parser.add_argument("name", type=kb_store.name_value)
    parser.add_argument("--remote", action="store_true", help="号池から推論時だけKBを挿入する")
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


def remote_command(native_args, remote, binding_id):
    overrides = json.loads(os.environ.get("KB_CODEX_CONFIG_OVERRIDES", "[]"))
    # Validate the inherited contract before registering anything.
    kb_codex.config_flags()
    provider = None
    for value in overrides:
        setting = tomllib.loads(value)
        if "model_provider" in setting:
            provider = setting["model_provider"]
    env = dict(os.environ)
    if provider is None:
        # Built-in openai ignores provider-table overrides. Use an invocation-only
        # provider for the normal fill-first pool route, with the same local model catalog.
        provider = "kb_pool"
        info = {
            "name": "Account Pool KB",
            "base_url": remote.origin + "/backend-api/codex",
            "env_key": "KB_NATIVE_POOL_KEY",
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": False,
        }
        table = "{ " + ", ".join(key + " = " + json.dumps(value) for key, value in info.items()) + " }"
        catalog = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "models_cache.json"
        if not catalog.is_file():
            raise ValueError("Codexのモデル一覧がありません。通常のcodexを一度起動してください")
        overrides += ['model_provider="kb_pool"', "model_providers.kb_pool=" + table,
                      "model_catalog_json=" + json.dumps(str(catalog.resolve()))]
        env["KB_NATIVE_POOL_KEY"] = remote.key
    overrides.append(f'model_providers.{provider}.http_headers.X-Codex-Parent-Thread-Id=' + json.dumps(binding_id))
    flags = [part for value in overrides for part in ("-c", value)]
    return with_config(native_args, flags), env


def run(args, config, snapshot, workspace, context, *, developer_text=None):
    if args.remote:
        remote = RemoteKB(config, snapshot, developer_text=developer_text)
        if context:
            remote.items.append({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": context}]})
        binding_id = str(uuid.uuid4())
        command, env = remote_command(args.native_args, remote, binding_id)
        # The original command creates its own session. Pool inheritance binds
        # that native session before its first inference, then persists the binding.
        remote.bind(binding_id)
        os.chdir(workspace)
        os.execvpe(command[0], command, env)
        return
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
