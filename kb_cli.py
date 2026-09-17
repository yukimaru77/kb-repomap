#!/usr/bin/env python3
"""Create and open repository KBs stored in user-selected Git repositories."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import kb_codex
import kb_store as store


ROOT = Path(__file__).resolve().parent
BUILD_ROOT = Path.home() / ".local/share/kb/builds"


def rebuild(name, info, commit, config):
    build_root = BUILD_ROOT / name / uuid.uuid4().hex
    build_root.mkdir(parents=True)
    env = dict(os.environ, KB_REPOMAP_HOME=str(build_root))
    command = [sys.executable, str(ROOT / "kb_repo_url.py"), info["repository_url"],
               *config.get("build_args", []), "--name", name, "--ref", commit,
               "--workspace", str(build_root / "repos"), "--no-mint"]
    subprocess.run(command, env=env, check=True)
    subprocess.run([sys.executable, str(ROOT / "kb_fork_mint.py"), "--name", name],
                   env=env, check=True)
    session_id = (build_root / "kb-session").read_text().strip()
    paths = list((Path.home() / ".codex/sessions").glob(f"*/*/*/rollout-*{session_id}.jsonl"))
    if len(paths) != 1:
        raise ValueError(f"生成したJSONLが見つかりません: {session_id}")
    return paths[0]


def should_rebuild(mode):
    if mode != "ask":
        return mode == "always"
    try:
        return input("基準ブランチに更新があります。KBを作り直しますか？ [y/N]: ").strip().lower() in {"y", "yes"}
    except EOFError:
        print("入力なし: 再作成せず、更新差分をセッションへ追加します。", file=sys.stderr)
        return False


def launch(args, config):
    loaded = store.find_kb(config, args.name, args.store)
    info, jsonl = loaded["info"], loaded["jsonl"]
    print(f"KB: {args.name} / store: {loaded['store']['name']}", flush=True)
    print(f"source: {info['repository_url']}@{info['source_commit']}", flush=True)
    head, context = store.source_update(info)
    if context:
        print(f"branch: {info['branch']} / KB: {info['source_commit'][:12]} → HEAD: {head[:12]}", flush=True)
        if should_rebuild(args.rebuild):
            jsonl = rebuild(args.name, info, head, config)
            info = {**info, "source_commit": head}
            revision = store.publish(loaded["store"], args.name, info, jsonl)
            print(f"KBを更新・保存しました: {revision}", flush=True)
            context = ""
        else:
            print(f"再作成せず更新差分を追加します: {len(context.encode()):,} bytes", flush=True)
    else:
        print("KBは基準ブランチと同じcommitです。", flush=True)
    workspace = Path(args.workspace).expanduser().resolve()
    session_id = kb_codex.start_session(jsonl, workspace, args.name, context,
                                        full_access=not args.no_yolo, prompt=args.prompt)
    print(f"session: {session_id}", flush=True)
    if args.session_only:
        return session_id
    if args.app:
        subprocess.run(["open", f"codex://threads/{session_id}"], check=True)
        return session_id
    command = ["codex", "resume"]
    if not args.no_yolo:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    command += ["-C", str(workspace), session_id]
    os.execvp(command[0], command)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="kb")
    commands = parser.add_subparsers(dest="command", required=True)
    stores = commands.add_parser("store", help="保存先Gitリポジトリを管理")
    actions = stores.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("name", type=store.name_value)
    add.add_argument("url")
    add.add_argument("--branch")
    actions.add_parser("list")
    remove = actions.add_parser("remove")
    remove.add_argument("name")
    listing = commands.add_parser("list", help="保存先にあるKBを一覧表示")
    listing.add_argument("--store")
    publish = commands.add_parser("publish", help="作成済みJSONLと元データ情報を保存")
    publish.add_argument("name", type=store.name_value)
    publish.add_argument("--store")
    publish.add_argument("--repository-url", required=True)
    publish.add_argument("--source-commit", required=True)
    publish.add_argument("--branch", required=True)
    publish.add_argument("--jsonl", type=Path, required=True)
    codex = commands.add_parser("codex", help="KBを取得して新規Codexセッションを起動")
    codex.add_argument("name", type=store.name_value)
    codex.add_argument("--store")
    codex.add_argument("--workspace", default=".")
    codex.add_argument("--rebuild", choices=("ask", "always", "never"), default="ask")
    mode = codex.add_mutually_exclusive_group()
    mode.add_argument("--session-only", action="store_true")
    mode.add_argument("--app", action="store_true")
    codex.add_argument("--no-yolo", action="store_true")
    codex.add_argument("--prompt")
    args = parser.parse_args(argv)
    config = store.read_config()
    if args.command == "store":
        entries = config.setdefault("stores", [])
        if args.action == "add":
            entry = {"name": args.name, "url": args.url}
            if args.branch:
                entry["branch"] = args.branch
            existing = next((i for i, v in enumerate(entries) if v["name"] == args.name), None)
            if existing is None:
                entries.append(entry)
            else:
                entries[existing] = entry
            store.write_config(config)
        elif args.action == "remove":
            config["stores"] = [v for v in entries if v["name"] != args.name]
            store.write_config(config)
        for entry in config["stores"]:
            print(f"{entry['name']}\t{entry['url']}")
    elif args.command == "list":
        for entry in store.configured_stores(config, args.store):
            repo, revision, _ = store.sync_store(entry)
            for filename in store.git(repo, "ls-tree", "-r", "--name-only", revision).stdout.splitlines():
                if filename.count("/") == 1 and filename.endswith("/info.json"):
                    info = json.loads(store.git(repo, "show", f"{revision}:{filename}").stdout)
                    print(f"{filename.split('/')[0]}\t{entry['name']}\t{info['source_commit'][:12]}\t{info['branch']}")
    elif args.command == "publish":
        info = {"repository_url": args.repository_url, "source_commit": args.source_commit, "branch": args.branch}
        selected = store.configured_stores(config, args.store)[0]
        print(store.publish(selected, args.name, info, args.jsonl))
    elif args.command == "codex":
        launch(args, config)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        detail = error.stderr or str(error)
        print(detail.decode(errors="replace") if isinstance(detail, bytes) else detail, file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"kb: {error}", file=sys.stderr)
        raise SystemExit(1)
