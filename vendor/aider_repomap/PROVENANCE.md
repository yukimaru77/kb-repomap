# Aider repository-map extraction

- Source: https://github.com/Aider-AI/aider
- Commit: `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`
- License: Apache-2.0; full original license in `LICENSE.txt`.
- Copied: `aider/repomap.py`, `aider/special.py`, and `aider/queries/`.

`repomap.py` retains the tree-sitter tags, Pygments reference extraction,
NetworkX PageRank, token-budget search, and grep-ast rendering. Changes:

- Relative imports for this standalone package.
- Removed the debug import, terminal spinner, and unrelated standalone/demo helpers.
- Added an optional cache directory so the scanned repository is not written to.
- Route parser diagnostics through the supplied IO adapter.

`special.py` and the query files are copied unchanged. Their upstream licenses and
notices are preserved. No Aider CLI, model client, editing, analytics, or login code
is included. `repo_map.py` supplies Git file discovery, `.aiderignore`, text IO,
and a local tiktoken counter.

The intended command is Aider's no-chat/no-mentioned-files map with
`--map-tokens 10000 --map-multiplier-no-files 1`. Aider's token budget is approximate
(sampling and 15% search tolerance); it is not a strict 10,000-token limit.
We use `cl100k_base` by default; `--encoding` permits another tiktoken encoding.
Aider itself chooses a tokenizer based on its model, so a different tokenizer or
user configuration can produce a different map. The interactive chat prefix is
omitted: only the repository map belongs in the KB.
