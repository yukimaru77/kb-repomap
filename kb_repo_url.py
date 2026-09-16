#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Clone a repository, make an Aider map, compact with v2, and mint a session."""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import kb_api

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_CONFIG = Path(__file__).with_name("config.yaml")
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}


def run(command, **kwargs):
    print("+ " + " ".join(map(str, command)), file=sys.stderr, flush=True)
    return subprocess.run(command, check=True, **kwargs)


def safe_slug(url):
    value = url.rstrip("/").rsplit("/", 1)[-1]
    if value.endswith(".git"):
        value = value[:-4]
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not value:
        raise SystemExit("could not derive a repository name from URL; pass --name")
    return value.lower()


def validate_source(value):
    path = Path(value).expanduser()
    if path.exists():
        return str(path.resolve())
    if value.startswith("git@") or value.startswith("ssh://"):
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "http", "git"} or not parsed.netloc:
        raise SystemExit("repository must be a local path or an http(s), ssh, git, or git@ URL")
    if parsed.username or parsed.password:
        raise SystemExit("do not embed credentials in the URL; use your Git credential helper")
    return value


def clone_repository(source, destination, ref):
    # KB ingestion only needs text source. Keep LFS pointers in the worktree so a
    # missing/retired binary object cannot block a build; kb_repo.py excludes the
    # corresponding binary/archive paths later.
    git_env = dict(os.environ)
    git_env["GIT_LFS_SKIP_SMUDGE"] = "1"
    if destination.exists():
        if not (destination / ".git").exists():
            raise SystemExit(f"destination exists but is not a Git repository: {destination}")
        dirty = subprocess.check_output(
            ["git", "-C", str(destination), "status", "--porcelain"], text=True
        ).strip()
        if dirty:
            raise SystemExit(f"managed clone has local changes; refusing to overwrite: {destination}")
        existing = subprocess.check_output(
            ["git", "-C", str(destination), "remote", "get-url", "origin"], text=True
        ).strip()
        if existing != source:
            raise SystemExit(f"existing clone has another origin: {existing}")
        run(["git", "-C", str(destination), "fetch", "origin", "--prune"], env=git_env)
        if ref:
            run(
                ["git", "-C", str(destination), "checkout", "--detach", ref],
                env=git_env,
            )
        else:
            remote_head = subprocess.check_output(
                ["git", "-C", str(destination), "symbolic-ref", "refs/remotes/origin/HEAD"],
                text=True,
            ).strip()
            run(
                ["git", "-C", str(destination), "checkout", "--detach", remote_head],
                env=git_env,
            )
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = ["git", "clone", "--no-tags", "--", source, str(destination)]
    run(command, env=git_env)
    if ref:
        run(
            ["git", "-C", str(destination), "checkout", "--detach", ref],
            env=git_env,
        )


def parse_simple_yaml(text):
    """Parse the deliberately small mapping-only schema used by config.yaml."""
    data = {}
    section = None
    for line_number, original in enumerate(text.splitlines(), 1):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        if "\t" in original:
            raise ValueError(f"line {line_number}: tabs are not supported")
        indent = len(original) - len(original.lstrip(" "))
        line = original.strip()
        if ":" not in line:
            raise ValueError(f"line {line_number}: expected key: value")
        key, raw = (part.strip() for part in line.split(":", 1))
        if not key:
            raise ValueError(f"line {line_number}: empty key")

        if raw and " #" in raw:
            raw = raw.split(" #", 1)[0].rstrip()
        if not raw:
            value = {}
        elif raw.lower() in {"true", "false"}:
            value = raw.lower() == "true"
        elif re.fullmatch(r"[-+]?\d[\d_]*", raw):
            value = int(raw.replace("_", ""))
        elif raw[:1] in {'"', "'"}:
            value = ast.literal_eval(raw)
            if not isinstance(value, str):
                raise ValueError(f"line {line_number}: quoted scalar must be a string")
        else:
            value = raw

        if indent == 0:
            data[key] = value
            section = key if isinstance(value, dict) else None
        elif indent == 2 and section and isinstance(data.get(section), dict):
            data[section][key] = value
        else:
            raise ValueError(f"line {line_number}: only one two-space nested mapping is supported")
    return data


def load_config(path):
    try:
        if yaml is not None:
            data = yaml.safe_load(path.read_text()) or {}
        elif path.suffix.lower() == ".json":
            data = json.loads(path.read_text())
        else:
            data = parse_simple_yaml(path.read_text())
    except (OSError, ValueError, SyntaxError, json.JSONDecodeError) as error:
        raise SystemExit(f"failed to read config {path}: {error}")
    except Exception as error:
        if yaml is not None and isinstance(error, yaml.YAMLError):
            raise SystemExit(f"failed to read config {path}: {error}")
        raise
    if not isinstance(data, dict):
        raise SystemExit("config root must be a YAML mapping")
    two_stage = data.get("two_stage") or {}
    if not isinstance(two_stage, dict):
        raise SystemExit("config.two_stage must be a mapping")
    config = {
        "chunk_tokens": data.get("chunk_tokens", 150_000),
        "two_stage_enabled": two_stage.get("enabled", True),
        "second_stage_budget_tokens": two_stage.get(
            "token_budget", data.get("chunk_tokens", 150_000)
        ),
        "model": data.get("model", kb_api.DEFAULT_MODEL),
        "effort": data.get("effort", kb_api.DEFAULT_EFFORT),
        "workers": data.get("workers", 10),
        "max_file_bytes": data.get("max_file_bytes", 600_000),
        "thin_blob_threshold": data.get("thin_blob_threshold", 2_000),
        "map_tokens": data.get("map_tokens", 10_000),
    }
    for key in ("chunk_tokens", "second_stage_budget_tokens", "workers", "max_file_bytes", "map_tokens"):
        if not isinstance(config[key], int) or config[key] <= 0:
            raise SystemExit(f"config.{key} must be a positive integer")
    if not isinstance(config["thin_blob_threshold"], int) or config["thin_blob_threshold"] < 0:
        raise SystemExit("config.thin_blob_threshold must be a non-negative integer")
    if not isinstance(config["two_stage_enabled"], bool):
        raise SystemExit("config.two_stage.enabled must be true or false")
    if not isinstance(config["model"], str) or not config["model"].strip():
        raise SystemExit("config.model must be a non-empty string")
    if config["effort"] not in EFFORTS:
        raise SystemExit(f"config.effort must be one of: {', '.join(sorted(EFFORTS))}")
    return config


def generate_repo_map(repo, output, map_tokens):
    from repo_map import generate_map

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".txt.tmp")
    temporary.write_text(generate_map(repo, map_tokens=map_tokens), encoding="utf-8")
    os.replace(temporary, output)

def main():
    parser = argparse.ArgumentParser(
        description="Repository URL -> Aider repo map -> v2 source blobs -> Codex fork session"
    )
    parser.add_argument("url", help="Git URL or local repository path")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="YAML configuration file")
    parser.add_argument("--name", help="KB/clone name (default: derived from URL)")
    parser.add_argument("--ref", help="branch, tag, or commit to check out")
    parser.add_argument("--workspace", default=str(Path(kb_api.STATE_ROOT) / "repos"))
    parser.add_argument("--repo-map", dest="repo_map", help="use an existing repository map")
    parser.add_argument("--map-tokens", type=int, help="repository map token budget (default: 10000)")
    parser.add_argument("--origin", help="pool origin, without /v1; or KB_POOL_ORIGIN")
    parser.add_argument("--key-file", help="pool client key file; or KB_POOL_KEY_FILE")
    parser.add_argument("--private-http", action="store_true", help="origin is inside a verified private tunnel")
    parser.add_argument(
        "--prelude-file", action="append", default=[],
        help="compress this non-repository context before source packs; repeatable",
    )
    parser.add_argument("--model", help="override config model")
    parser.add_argument("--effort", choices=sorted(EFFORTS), help="override config effort")
    parser.add_argument("--budget-tokens", type=int, help="override config chunk_tokens")
    parser.add_argument("--workers", type=int, help="override config workers")
    parser.add_argument("--max-file-bytes", type=int, help="override config max_file_bytes")
    two_stage = parser.add_mutually_exclusive_group()
    two_stage.add_argument(
        "--two-stage", dest="two_stage", action="store_true",
        help="merge first-stage blobs once more",
    )
    two_stage.add_argument(
        "--no-two-stage", dest="two_stage", action="store_false",
        help="keep first-stage blobs without a second compaction",
    )
    parser.set_defaults(two_stage=None)
    parser.add_argument(
        "--second-stage-budget-tokens", type=int,
        help="measured first-stage output tokens per second-stage compaction",
    )
    parser.add_argument("--dry-run", action="store_true", help="plan packs without API calls")
    parser.add_argument("--map-only", dest="map_only", action="store_true")
    parser.add_argument("--no-mint", action="store_true")
    parser.add_argument("--refresh-map", dest="refresh_map", action="store_true")
    args = parser.parse_args()

    config = load_config(Path(args.config).expanduser().resolve())
    if args.origin:
        os.environ["KB_POOL_ORIGIN"] = args.origin
    if args.key_file:
        os.environ["KB_POOL_KEY_FILE"] = str(Path(args.key_file).expanduser().resolve())
    if args.private_http:
        os.environ["KB_POOL_PRIVATE_HTTP"] = "1"
    if not args.dry_run and not args.map_only:
        kb_api.pool_configuration()
    map_tokens = args.map_tokens if args.map_tokens is not None else config["map_tokens"]
    model = args.model or config["model"]
    effort = args.effort or config["effort"]
    budget_tokens = args.budget_tokens or config["chunk_tokens"]
    workers = args.workers or config["workers"]
    max_file_bytes = args.max_file_bytes or config["max_file_bytes"]
    two_stage_enabled = (
        config["two_stage_enabled"] if args.two_stage is None else args.two_stage
    )
    second_stage_budget_tokens = (
        args.second_stage_budget_tokens or config["second_stage_budget_tokens"]
    )
    if second_stage_budget_tokens < 1:
        raise SystemExit("--second-stage-budget-tokens must be positive")

    source = validate_source(args.url)
    name = args.name or safe_slug(source)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise SystemExit("--name may contain only letters, digits, dot, underscore, and hyphen")
    repo = Path(args.workspace).expanduser().resolve() / name
    clone_repository(source, repo, args.ref)

    repo_map = (Path(args.repo_map).expanduser().resolve() if args.repo_map else
                Path(kb_api.STATE_ROOT) / name / "repository-map.txt")
    repo_map_commit = repo_map.with_suffix(".commit")
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    recorded = repo_map_commit.read_text().strip() if repo_map_commit.exists() else ""
    if not args.repo_map and (args.refresh_map or not repo_map.exists() or recorded != f"{head}:{map_tokens}"):
        generate_repo_map(repo, repo_map, map_tokens)
        repo_map_commit.write_text(f"{head}:{map_tokens}\n")
    if not repo_map.is_file():
        raise SystemExit(f"repo_map not found: {repo_map}")
    print(f"repository: {repo}", file=sys.stderr)
    print(f"repo_map:   {repo_map}", file=sys.stderr)
    if args.map_only:
        return

    command = [
        sys.executable, str(Path(__file__).with_name("kb_repo.py")),
        "--repo", str(repo), "--repo-map", str(repo_map), "--name", name,
        "--budget-tokens", str(budget_tokens), "--workers", str(workers),
        "--max-file-bytes", str(max_file_bytes),
        "--thin-threshold", str(config["thin_blob_threshold"]),
        "--model", model, "--effort", effort,
    ]
    for prelude in args.prelude_file:
        command += ["--prelude-file", str(Path(prelude).expanduser().resolve())]
    if args.dry_run:
        command.append("--dry-run")
    run(command)
    if not args.dry_run and two_stage_enabled:
        run([
            sys.executable, str(Path(__file__).with_name("kb_api.py")), "merge-old",
            "--name", name, "--budget-tokens", str(second_stage_budget_tokens),
            "--model", model, "--effort", effort, "--workers", str(workers),
        ])
    if not args.dry_run and not args.no_mint:
        run([sys.executable, str(Path(__file__).with_name("kb_fork_mint.py")), "--name", name])


if __name__ == "__main__":
    main()
