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
Fork-session files are also tested in an isolated temporary directory. The synthetic
live check above does not launch/resume a real Codex session; the separate native
fork test below does. No Codex settings or authentication files are read or changed
by map/blob generation.

## Native Codex fork with 35 independent blobs (2026-09-17)

Generated the user's Octane KB at source commit
`6ac751c8c559a374910788a39234af4aa77c154a` using engine commit
`3f167413a15d7005203d6db9d79f86a984ea1b34`, 12 workers, and no second stage.
The build produced 35 encrypted compaction items from 1,458 included files.
Clone, map, compaction, and session minting took 324.815 seconds in total.

The installed `codex-cli 0.153.4` app-server accepted `thread/fork` with the minted
session ID and then `turn/start` on that fork. No model/provider/config overrides
were passed. The runtime selected `gpt-6-astra` with the `openai` provider.
The fork's persisted rollout retained all 35 original encrypted payloads in order,
and its answer turn completed without tools or runtime errors.

```json
{
  "source_session_id": "01a0ab0b-ce85-70b5-9189-47734a1155d7",
  "fork_session_id": "01a0ad7b-94ee-71a0-9722-e618f09208fc",
  "fork_blob_count": 35,
  "all_source_blobs_preserved": true,
  "turn_status": "completed",
  "input_tokens": 141141,
  "output_tokens": 921,
  "source_config_binary_unchanged": true
}
```

The runtime's input count includes the fork's surrounding instructions, tools,
and test question. It is distinct from the 121,091 output tokens reported across
the retained blobs' original compaction calls.

Of three source-specific questions asked using only the loaded knowledge, the
answer correctly recalled the BYOK cipher/layout and credit-expiry worker's
minimum interval, but reported the auto-charge worker's local deduplication
mechanism as unknown. This demonstrates a usable session with multiple blobs,
not lossless recall of every source detail or compatibility with future builds.

Private local evidence is stored under
`state/benchmarks/octane-6ac751c-20260917/`: `verify-session.py`,
`fork-check-result.json`, `fork-check-events.jsonl`, and the earlier `result.json`.
The original KB rollout, Codex config, and executable hashes matched before/after.

## Portable KB snapshots and fresh local sessions (2026-09-21)

Converted the existing pi snapshot to a JSON array of two encrypted compaction
items and its KB user charter. The encrypted items and their order were retained;
session metadata and base instructions were not copied.

Both normal and remote startup were exercised with the installed Codex app-server.
Each created a fresh thread using local configuration and answered `openpi / π0`
from the KB without tools. Neither thread had a `forked_from_id`. The normal
thread persisted the two injected blobs; the remote thread persisted zero KB
blobs and used the same three-item remote snapshot as before conversion.

The older fork validations above describe the legacy standalone mint workflow,
not the current `kb codex` startup path.
