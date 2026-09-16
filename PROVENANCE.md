# Sources

The KB packing, two-stage grouping, checkpoints, and fork-session writer are
adapted from `yukimaru77/kb-multiblob` commit
`9494b3939722721dd719933f869760bf7b4310a3` (MIT; original notice retained in `LICENSE`).

The native Responses compaction stream handling is adapted from
`yukimaru77/codex-account-pool`, commit `aa7b180d4b660f172ac1b79976e7614e5bc7df5c`, script `scripts/compact-jsonl.py`.
The pool is an external HTTP service; its server code is not included here.

Aider's repository-map engine has its own Apache-2.0 license and pinned source:
see `vendor/aider_repomap/PROVENANCE.md` and `vendor/aider_repomap/LICENSE.txt`.
