"""Register a fixed KB snapshot with the account pool, without editing Codex."""
import argparse
import json
import os
from pathlib import Path
import urllib.request

from kb_api import NoRedirect, pool_configuration
from kb_items import load_session_items


class RemoteKB:
    def __init__(self, config, jsonl, *, developer_text=None):
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
        # Session metadata belongs to Codex. Bind developer guidance and the
        # portable memories; never import the producer's session configuration.
        self.items = load_session_items(jsonl, developer_text=developer_text)

    def bind(self, session_id, *, include_guidance=True):
        # The legacy app-server launcher persists the first item locally to
        # create a resumable rollout. Native launches keep it in the binding.
        # Store dev.txt remains in both bindings, so it is never lost or doubled.
        items = self.items if include_guidance else self.items[1:]
        request = urllib.request.Request(
            self.origin + "/_pool/kb/bind",
            data=json.dumps({"session_id": session_id, "items": items}, ensure_ascii=False).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            result = json.load(response)
        print(f"Remote KB: {result['snapshot_id']} / {result['item_count']} items", flush=True)
        return result
