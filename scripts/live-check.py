#!/usr/bin/env python3
"""Small opt-in live check: two native blobs -> one native blob -> recall.

Makes four inference/compaction calls. Uses only synthetic source and the pool
client key. Does not read or change Codex settings, credentials, or sessions.
"""
import argparse
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_api
from kb_repo import blob_source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--private-http", action="store_true")
    parser.add_argument("--model", default=kb_api.DEFAULT_MODEL)
    parser.add_argument("--effort", default=kb_api.DEFAULT_EFFORT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ["KB_POOL_ORIGIN"] = args.origin
    os.environ["KB_POOL_KEY_FILE"] = args.key_file
    if args.private_http:
        os.environ["KB_POOL_PRIVATE_HTTP"] = "1"
    markers = ["KB_ALPHA_" + secrets.token_hex(6), "KB_BETA_" + secrets.token_hex(6)]
    blobs = []
    measurements = []
    for index, marker in enumerate(markers):
        path = PurePosixPath(f"module_{index}.py")
        source = blob_source(
            "module_0.py: def alpha_token()\nmodule_1.py: def beta_token()\n",
            [path], {path: f"def {'alpha' if index == 0 else 'beta'}_token():\n    return '{marker}'\n"},
        )
        items, seconds, usage = kb_api.compact([kb_api.u(source)], args.model, args.effort)
        blobs.extend(items)
        measurements.append({"stage": 1, "seconds": seconds, "usage": usage})
        print(f"stage 1/{index + 1}: encrypted blob received", flush=True)
    merged, seconds, usage = kb_api.compact(
        [kb_api.u("Combine both source modules; retain their exact function names and return values."), *blobs],
        args.model, args.effort,
    )
    measurements.append({"stage": 2, "seconds": seconds, "usage": usage})
    print("stage 2: encrypted blob received", flush=True)
    request = {"model": args.model, "instructions": "Answer using the repository knowledge.",
               "input": [*merged, kb_api.u("What exact strings do alpha_token() and beta_token() return? Give both strings.")],
               "stream": True, "store": False, "reasoning": {"effort": "low"}}
    text = []
    completed = False
    with kb_api.http("/responses", request, stream=True) as response:
        for event in kb_api.events(response):
            if event.get("type") == "response.output_text.delta":
                text.append(event.get("delta", ""))
            if event.get("type") == "response.completed":
                completed = True
    recovered = [marker in "".join(text) for marker in markers]
    result = {"model": args.model, "effort": args.effort, "stage1_blobs": len(blobs),
              "stage2_blobs": len(merged), "encrypted_content_present": bool(merged[0]["encrypted_content"]),
              "continued_response_completed": completed, "markers_recovered": recovered,
              "measurements": measurements, "ok": completed and all(recovered)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
