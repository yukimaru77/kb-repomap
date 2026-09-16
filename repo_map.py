#!/usr/bin/env python3
"""Standalone extraction of Aider's --show-repo-map (no LLM calls)."""

import argparse
from contextlib import redirect_stdout
import hashlib
from pathlib import Path
import subprocess
import sys

import pathspec
import tiktoken

from vendor.aider_repomap.repomap import RepoMap


class MapIO:
    def read_text(self, filename):
        try:
            return Path(filename).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return None

    def tool_output(self, message):
        print(message, file=sys.stderr)

    tool_warning = tool_output
    tool_error = tool_output


class TokenCounter:
    def __init__(self, encoding="cl100k_base"):
        self.encoding = tiktoken.get_encoding(encoding)

    def token_count(self, text):
        return len(self.encoding.encode(text, disallowed_special=()))


def repository_files(repo):
    """Aider's no-chat file set: HEAD plus index, minus .aiderignore."""
    repo = Path(repo).resolve()
    staged = subprocess.check_output(["git", "-C", str(repo), "ls-files", "-z"])
    head = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-rz", "--name-only", "HEAD"],
        capture_output=True, check=False,
    )
    names = set((staged + head.stdout).decode("utf-8", errors="surrogateescape").split("\0"))
    names.discard("")
    ignore = repo / ".aiderignore"
    spec = pathspec.PathSpec.from_lines(
        "gitwildmatch", ignore.read_text().splitlines() if ignore.exists() else []
    )
    return [str(repo / name) for name in sorted(names) if not spec.match_file(name)]


def generate_map(repo, map_tokens=10_000, multiplier=1, cache_dir=None, encoding="cl100k_base"):
    repo = Path(repo).resolve()
    if cache_dir is None:
        identity = hashlib.sha256(str(repo).encode()).hexdigest()[:20]
        cache_dir = Path("~/.cache/kb-repomap").expanduser() / identity
    mapper = RepoMap(
        map_tokens=map_tokens, root=str(repo), main_model=TokenCounter(encoding),
        io=MapIO(), map_mul_no_files=multiplier,
        max_context_window=int(map_tokens * max(1, multiplier)) + 4096,
        refresh="always", cache_dir=cache_dir,
    )
    try:
        # Diagnostics from dependencies never become part of the map artifact.
        with redirect_stdout(sys.stderr):
            return mapper.get_repo_map(set(), repository_files(repo)) or ""
    finally:
        if hasattr(mapper.TAGS_CACHE, "close"):
            mapper.TAGS_CACHE.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--show-repo-map", action="store_true", help="print the map (the default action)")
    parser.add_argument("--map-tokens", type=int, default=10_000)
    parser.add_argument("--map-multiplier-no-files", type=float, default=1)
    parser.add_argument("--encoding", default="cl100k_base")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = generate_map(args.repo, args.map_tokens, args.map_multiplier_no_files, encoding=args.encoding)
    if args.output:
        args.output.write_text(result, encoding="utf-8")
    else:
        sys.stdout.write(result)


if __name__ == "__main__":
    main()
