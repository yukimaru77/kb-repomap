#!/usr/bin/env python3
"""Create and open repository KBs stored in user-selected Git repositories."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

import kb_codex
import kb_store as store
from kb_items import dump_items, load_items
from kb_api import COMPACTION_TYPES
from kb_fork_mint import CHARTER
from kb_source import source_address


ROOT = Path(__file__).resolve().parent
BUILD_ROOT = Path.home() / ".local/share/kb/builds"


def file_argument(value):
    return store.jsonl_filename(value if value.endswith((".json", ".jsonl")) else value + ".json")


def rebuild(name, info, commit, config):
    build_root = BUILD_ROOT / name / uuid.uuid4().hex
    build_root.mkdir(parents=True)
    env = dict(os.environ, KB_REPOMAP_HOME=str(build_root))
    command = [sys.executable, str(ROOT / "kb_repo_url.py"), info["repository_url"],
               *config.get("build_args", []), "--name", name, "--ref", commit,
               "--workspace", str(build_root / "repos"), "--no-mint"]
    subprocess.run(command, env=env, check=True)
    state = json.loads((build_root / name / "state.json").read_text())
    items = [item for item in state.get("blobs") or state.get("base_items", [])
             if item.get("type") in COMPACTION_TYPES]
    items.append({"type": "message", "role": "user",
                  "content": [{"type": "input_text", "text": CHARTER}]})
    snapshot = build_root / name / "kb.json"
    dump_items(snapshot, items)
    return snapshot


def should_rebuild(mode):
    if mode != "ask":
        return mode == "always"
    try:
        return input("基準ブランチに更新があります。KBを作り直しますか？ [y/N]: ").strip().lower() in {"y", "yes"}
    except EOFError:
        print("入力なし: 再作成せず、更新差分をセッションへ追加します。", file=sys.stderr)
        return False


def prompt_value(label, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip() or default
    if not value:
        raise ValueError(f"{label}を入力してください")
    return value


def register(args, config):
    name = args.name or store.name_value(prompt_value("KB名"))
    source = getattr(args, "source", None)
    host, path = getattr(args, "host", None), getattr(args, "path", None)
    repository_url = source_address(source or (prompt_value("元Gitリポジトリ（URL / host:/絶対パス）") if not host and not path else None), host=host, path=path)
    branch = getattr(args, "branch", None) or prompt_value("基準ブランチ", "main")
    entries = config.setdefault("stores", [])
    for entry in entries:
        print(f"保存先 {entry['name']}: {entry['url']}")
    print("自分専用の保存先には、プライベートリポジトリを作成して使うことをおすすめします。")
    url = prompt_value("保存先GitリポジトリのURL", entries[0]["url"] if entries else None)
    selected = next((entry for entry in entries if entry["url"] == url), None)
    if selected is None:
        base = re.sub(r"[^A-Za-z0-9._-]+", "-", url.rstrip("/").rsplit("/", 1)[-1]
                      .rsplit(":", 1)[-1].removesuffix(".git")).strip(".-") or "store"
        candidate = base
        number = 2
        while any(entry["name"] == candidate for entry in entries):
            candidate = f"{base}-{number}"
            number += 1
        selected = {"name": candidate, "url": url}
    info = {"repository_url": repository_url, "source_commit": None, "branch": branch}
    revision = store.publish(selected, name, info, create_only=True)
    if selected not in entries:
        entries.append(selected)
        store.write_config(config)
    print(f"登録しました: {name} / store: {selected['name']} / {revision}")
    print(f"作成: kb create {name} --store {selected['name']}")


def create_kb(name, loaded, commit, config, filename="latest.json"):
    print(f"KBを作成します: {name}@{commit} / store: {loaded['store']['name']}", flush=True)
    jsonl = rebuild(name, loaded["info"], commit, config)
    info = {**loaded["info"], "source_commit": commit}
    revision = store.publish(loaded["store"], name, info, jsonl, filename=filename)
    print(f"KBを作成・保存しました: {name}/{filename} / {revision}", flush=True)
    return jsonl


def publish_paper(args, config):
    run = args.run.expanduser().resolve()
    manifest = json.loads((run / "manifest.json").read_text())
    result = json.loads((run / "result.json").read_text())
    snapshot = run / "kb.json"
    items = load_items(snapshot)
    references, main = result["references"], result["main_blobs"]
    if (not isinstance(references, int) or not isinstance(main, int)
            or references < 1 or main < 1 or len(items) != references + main
            or result.get("items") != len(items)
            or any(item.get("type") not in COMPACTION_TYPES for item in items)):
        raise ValueError("paper-kbの完了結果とblob数が一致しません")
    source_hash = manifest["source_sha256"]
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("paper-kbの入力ハッシュが不正です")
    source_git = manifest.get("source_git") or {}
    commit = source_git.get("source_commit", "")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) or not source_git.get("repository_url"):
        raise ValueError("論文KBには元Gitリポジトリとコミットが必要です。新版paper-kbで出力を更新してください")
    info = {"source_kind": "paper", "source_sha256": source_hash,
            "repository_url": source_address(source_git["repository_url"]),
            "source_commit": commit, "branch": source_git.get("branch"),
            "subdir": source_git.get("subdir", "."),
            "references": references, "main_blobs": main,
            "build": {key: manifest[key] for key in ("model", "effort", "budget", "chunk_tokens") if key in manifest}}
    store.verify_source_commit(info)
    selected = store.configured_stores(config, args.store)[0]
    revision = store.publish(selected, args.name, info, snapshot, filename=args.file)
    print(f"論文KBを保存しました: {args.name}/{args.file} / store: {selected['name']} / {revision}")


def launch(args, config):
    loaded = store.find_kb(config, args.name, args.store, filename=args.file)
    info, jsonl = loaded["info"], loaded["jsonl"]
    print(f"KB: {args.name}/{args.file} / store: {loaded['store']['name']}", flush=True)
    paper = info.get("source_kind") == "paper"
    if paper:
        print(f"source: paper / SHA256 {info['source_sha256']} / 引用 {info['references']} / 本論文blob {info['main_blobs']}", flush=True)
        if args.rebuild == "always":
            raise ValueError("論文KBの再作成はpaper-kbを実行し、--publishで保存してください")
        head, context = None, ""
        if info.get("repository_url") and info.get("source_commit"):
            print(f"Git: {info['repository_url']}@{info['source_commit']} / {info.get('subdir', '.')}", flush=True)
            head = store.source_head(info) if info.get("branch") else info["source_commit"]
            if head != info["source_commit"]:
                print("元Gitに更新があります。保存済みの論文KBで開始します。更新する場合はpaper-kbで再作成・保存してください。", flush=True)
        else:
            print("旧形式の論文KBです。次回保存時にはGit入力から再作成してください。", flush=True)
    else:
        print(f"source: {info['repository_url']}@{info['source_commit']}", flush=True)
        head, context = store.source_update(info)
    if context:
        print(f"branch: {info['branch']} / KB: {info['source_commit'][:12]} → HEAD: {head[:12]}", flush=True)
        if should_rebuild(args.rebuild):
            jsonl = create_kb(args.name, loaded, head, config, args.file)
            context = ""
        else:
            print(f"再作成せず更新差分を追加します: {len(context.encode()):,} bytes", flush=True)
    elif not paper:
        print("KBは基準ブランチと同じcommitです。", flush=True)
    workspace = Path(args.workspace).expanduser().resolve()
    options = {}
    if getattr(args, "remote", False):
        from kb_remote import RemoteKB
        options["remote"] = RemoteKB(config, jsonl)
    session_id = kb_codex.start_session(jsonl, workspace, args.name, context,
                                        full_access=not args.no_yolo, prompt=args.prompt, **options)
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
    registration = commands.add_parser("register", help="URL・ブランチ・保存先を対話登録")
    registration.add_argument("name", nargs="?", type=store.name_value, help="KB名（省略すると質問）")
    registration.add_argument("--source", help="元Git URLまたはローカルGitパス")
    registration.add_argument("--host", help="SSHマシン名（user@hostやSSH別名）")
    registration.add_argument("--path", help="SSH先のGitリポジトリ絶対パス")
    registration.add_argument("--branch", help="基準ブランチ（省略時は質問）")
    creation = commands.add_parser("create", help="登録済みKBを基準ブランチから作成・保存")
    creation.add_argument("name", type=store.name_value)
    creation.add_argument("--store")
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
    publish = commands.add_parser("publish", help="KB項目列と元データ情報を保存（旧JSONLも読込可）")
    publish.add_argument("name", type=store.name_value)
    publish.add_argument("--store")
    publish.add_argument("--repository-url", required=True)
    publish.add_argument("--source-commit", required=True)
    publish.add_argument("--branch", required=True)
    publish.add_argument("--kb", "--jsonl", dest="jsonl", metavar="PATH", type=Path, required=True)
    paper_publish = commands.add_parser("publish-paper", help="paper-kbの完成結果を共通保存先に保存")
    paper_publish.add_argument("name", type=store.name_value)
    paper_publish.add_argument("--run", type=Path, required=True)
    paper_publish.add_argument("--store")
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
    codex.add_argument("--remote", action="store_true", help="号池にKBを登録し、推論時だけ挿入する")
    for command in (creation, publish, paper_publish, codex):
        command.add_argument("--file", default="latest.json", type=file_argument,
                             help="KBファイル名（.jsonは省略可、既定: latest.json、旧.jsonlも読込可、同名は上書き）")
    args = parser.parse_args(argv)
    config = store.read_config()
    if args.command == "register":
        register(args, config)
    elif args.command == "create":
        loaded = store.find_kb(config, args.name, args.store, download=False)
        if loaded["info"].get("source_kind") == "paper":
            raise ValueError("論文KBはpaper-kbで作成し、--publishで保存してください")
        create_kb(args.name, loaded, store.source_head(loaded["info"]), config, args.file)
    elif args.command == "store":
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
            for filename in store.git(repo, "ls-tree", "-r", "--name-only", "-z", revision).stdout.split("\0"):
                if filename.count("/") == 1 and (filename.endswith("/info.json") or filename.endswith(".info.json")):
                    info = json.loads(store.git(repo, "show", f"{revision}:{filename}").stdout)
                    commit = store.source_revision(info)
                    name, metadata = filename.split("/")
                    jsonl_name = "latest.json" if metadata == "info.json" else metadata.removesuffix(".info.json") + ".json"
                    if commit:
                        jsonl_name = store.snapshot_filename(repo, revision, name, jsonl_name)
                    branch = "paper" if info.get("source_kind") == "paper" else info["branch"]
                    print(f"{name}\t{entry['name']}\t{commit[:12] if commit else '未作成'}\t{branch}\t{jsonl_name}")
    elif args.command == "publish-paper":
        publish_paper(args, config)
    elif args.command == "publish":
        info = {"repository_url": args.repository_url, "source_commit": args.source_commit, "branch": args.branch}
        selected = store.configured_stores(config, args.store)[0]
        print(store.publish(selected, args.name, info, args.jsonl, filename=args.file))
    elif args.command == "codex":
        launch(args, config)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        detail = error.stderr or str(error)
        print(detail.decode(errors="replace") if isinstance(detail, bytes) else detail, file=sys.stderr)
        raise SystemExit(1)
    except (ValueError, RuntimeError, OSError, EOFError) as error:
        print(f"kb: {error}", file=sys.stderr)
        raise SystemExit(1)
