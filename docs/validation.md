# Validation — 2026-09-16

Environment: macOS arm64, Python 3.12.13, dependencies pinned in `uv.lock`.

## Aider extraction

```sh
uv run python scripts/check-aider-parity.py /path/to/pinned-aider /path/to/kb-multiblob
```

Aider commit: `5dc9490bb35f9729ef2c95d00a19ccd30c26339c`.
Target commit: `9494b3939722721dd719933f869760bf7b4310a3`.
Both engines receive the same no-chat file set and `cl100k_base` token counter.
Only upstream debug/spinner UI is replaced by quiet adapters for this comparison.

```json
{"budget":512,"matches_upstream":true,"bytes":1824,"tokens":560}
{"budget":10000,"matches_upstream":true,"bytes":10380,"tokens":2825}
```

The 512 case illustrates Aider's existing approximate-budget tolerance.
This checks extracted-engine parity, not arbitrary Aider model/config settings.

## Unit and HTTP integration

```sh
uv run python -m unittest discover -s tests -v
```

Result: `Ran 39 tests in 10.911s` / `OK`.

Covers map generation without inference, `.aiderignore`, source packing,
SSE framing and unknown events, native compaction types and unknown item fields,
completion/failure handling, existing retries, checkpoint order, two-stage grouping,
fork-session blob preservation and ordinals. A local HTTP server exercises the
actual CLI from clone/map through both compaction stages and a resumed run.
All requests go to `/_pool/rr/responses`; no legacy compact route is used.

The default run now creates only first-stage blobs. HTTP integration tests then
opt into count-based grouping (two blobs) or token-based grouping (6,000 measured
output tokens), and verify that first-stage packs are reused. Grouping tests cover
unpaired blobs, preservation of order, prelude/already-merged exclusions, and count
grouping without token measurements. Token grouping retains its measured-token rule.

## Live pool / upstream test

```sh
uv run python scripts/live-check.py --origin http://TAILSCALE-HOST:18473 \
  --private-http --key-file /path/to/pool-client.key --output state/live-check.json
```

Executed against the user's running Tailscale pool with `gpt-5.6-sol`, `high`.
Two synthetic source modules had randomly generated return values not present in
the follow-up question. Each was compacted to an encrypted blob; both blobs were
then compacted together. The follow-up received only the merged blob and a question.

```json
{
  "stage1_blobs": 2,
  "stage2_blobs": 1,
  "encrypted_content_present": true,
  "continued_response_completed": true,
  "markers_recovered": [true, true],
  "ok": true
}
```

Compaction elapsed seconds: 8.1, 6.4, 5.9.
Reported output tokens: 286, 224, 175.
No plaintext-summary or legacy-endpoint fallback was used.

The live check covers small synthetic inputs and two-stage recall. It does not
establish knowledge completeness for 150K-token packs or all repositories.
Fork-session files are tested in an isolated temporary directory; this change's
live check does not launch/resume a real Codex session. No Codex settings or
authentication files are read or changed by map/blob generation.
