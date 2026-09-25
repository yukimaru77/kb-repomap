#!/usr/bin/env python3
"""Measure repository source tokens without invoking Codex."""

import argparse
from collections import Counter
import json
from pathlib import Path

import kb_api
from kb_repo import file_block, git, repomix_candidates, skip_reason, tracked_files


def source_stats(repo, max_file_bytes):
    repo = Path(repo).expanduser().resolve()
    try:
        commit = git(repo, "rev-parse", "HEAD")
    except Exception as error:
        raise SystemExit(f"not a git repository: {repo}") from error

    included = 0
    source_tokens = 0
    skipped = Counter()
    candidates = repomix_candidates(repo)
    for rel in tracked_files(repo):
        path = repo / rel.as_posix()
        if not path.is_file():
            continue
        if rel not in candidates:
            skipped["repomix filter"] += 1
            continue
        data = path.read_bytes()
        reason = skip_reason(rel, data, max_file_bytes)
        if reason:
            skipped[reason] += 1
            continue
        included += 1
        source_tokens += kb_api.est_tokens(
            file_block(rel.as_posix(), data.decode("utf-8"))
        )
    return {
        "repository": str(repo),
        "commit": commit,
        "included_files": included,
        "skipped_files": sum(skipped.values()),
        "source_total_tokens": source_tokens,
        "skip_reasons": dict(sorted(skipped.items())),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--max-file-bytes", type=int, default=600_000)
    args = parser.parse_args()
    if args.max_file_bytes < 1:
        raise SystemExit("--max-file-bytes must be positive")
    print(json.dumps(
        source_stats(args.repo, args.max_file_bytes),
        ensure_ascii=False,
        separators=(",", ":"),
    ))


if __name__ == "__main__":
    main()
