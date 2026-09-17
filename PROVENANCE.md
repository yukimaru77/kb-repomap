# Sources

The KB packing, two-stage grouping, checkpoints, and fork-session writer are
adapted from `yukimaru77/kb-multiblob` commit
`9494b3939722721dd719933f869760bf7b4310a3` (MIT; original notice retained in `LICENSE`).

The native Responses compaction stream handling is adapted from
`yukimaru77/codex-account-pool`, commit `aa7b180d4b660f172ac1b79976e7614e5bc7df5c`, script `scripts/compact-jsonl.py`.
The pool is an external HTTP service; its server code is not included here.

The small app-server client in `kb_codex.py` is adapted from the MIT-licensed
KB CLI snapshot in `yukimaru77/kb-toolchain`, commit
`eb45e95` (`cli/bin/kb`). The original copyright notice and license are
retained in `LICENSE.kb-cli`. It does not use the snapshot's catalog,
organization-specific APIs, credentials, or execution services.

Aider's repository-map engine has its own Apache-2.0 license and pinned source:
see `vendor/aider_repomap/PROVENANCE.md` and `vendor/aider_repomap/LICENSE.txt`.
