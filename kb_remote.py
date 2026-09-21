"""Register a fixed KB snapshot with the account pool, without editing Codex."""
import argparse
import json
import os
from pathlib import Path
import urllib.request

from kb_api import NoRedirect, pool_configuration
from kb_items import load_items


class RemoteKB:
    def __init__(self, config, jsonl):
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        parser.add_argument("--pool-config")
        parser.add_argument("--origin")
        parser.add_argument("--key-file")
        parser.add_argument("--private-http", action="store_true")
        args, _ = parser.parse_known_args(config.get("build_args", []))
        environ = dict(os.environ)
        if args.pool_config is not None:
            environ.setdefault("KB_POOL_CONFIG", args.pool_config)
        if args.origin:
            environ.setdefault("KB_POOL_ORIGIN", args.origin)
        if args.key_file:
            environ.setdefault("KB_POOL_KEY_FILE", args.key_file)
        if args.private_http:
            environ.setdefault("KB_POOL_PRIVATE_HTTP", "1")
        base, self.key = pool_configuration(environ)
        self.origin = base.removesuffix("/_pool/rr")
        # Session metadata belongs to Codex. Only model-visible response items
        # (all independent blobs, followed by the existing KB charter) go remote.
        self.items = load_items(jsonl)

    def bind(self, session_id):
        request = urllib.request.Request(
            self.origin + "/_pool/kb/bind",
            data=json.dumps({"session_id": session_id, "items": self.items}, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            result = json.load(response)
        print(f"Remote KB: {result['snapshot_id']} / {result['item_count']} items", flush=True)
        return result
