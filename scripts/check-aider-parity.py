#!/usr/bin/env python3
"""Compare this extraction with a pinned upstream checkout; no Aider installation."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from repo_map import MapIO, TokenCounter, generate_map, repository_files

PIN = "5dc9490bb35f9729ef2c95d00a19ccd30c26339c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path)
    parser.add_argument("repo", type=Path)
    args = parser.parse_args()
    assert subprocess.check_output(["git", "-C", str(args.upstream), "rev-parse", "HEAD"], text=True).strip() == PIN
    package = types.ModuleType("aider")
    package.__path__ = [str(args.upstream / "aider")]
    package.__spec__ = importlib.util.spec_from_file_location(
        "aider", args.upstream / "aider" / "__init__.py", submodule_search_locations=package.__path__
    )
    sys.modules["aider"] = package
    # Replace only terminal/debug UI. Ranking, parsing, rendering, and queries are upstream.
    waiting = types.ModuleType("aider.waiting")
    class QuietSpinner:
        def __init__(self, *args): pass
        def step(self, *args): pass
        def end(self): pass
    waiting.Spinner = QuietSpinner
    sys.modules["aider.waiting"] = waiting
    from aider.repomap import RepoMap as Upstream
    root = args.repo.resolve()
    with tempfile.TemporaryDirectory() as temp:
        for budget in (512, 10_000):
            Upstream.TAGS_CACHE_DIR = str(Path(temp) / "upstream")
            original = Upstream(map_tokens=budget, root=str(root), main_model=TokenCounter(),
                                io=MapIO(), map_mul_no_files=1, max_context_window=budget + 4096)
            try:
                expected = original.get_repo_map(set(), repository_files(root)) or ""
            finally:
                original.TAGS_CACHE.close()
            actual = generate_map(root, budget, cache_dir=Path(temp) / "extracted")
            assert actual == expected, f"map differs at budget={budget}"
            print(json.dumps({"budget": budget, "matches_upstream": True,
                              "bytes": len(actual.encode()), "tokens": TokenCounter().token_count(actual)}))


if __name__ == "__main__":
    main()
