import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ForkMintTest(unittest.TestCase):
    def test_minted_rollout_has_contiguous_ordinals(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            kb_home = root / "kb"
            template_dir = home / ".codex" / "sessions" / "2026" / "08" / "12"
            template_dir.mkdir(parents=True)
            (template_dir / "rollout-template.jsonl").write_text(
                json.dumps({
                    "timestamp": "2026-08-12T00:00:00.000Z",
                    "type": "session_meta",
                    "ordinal": 0,
                    "payload": {
                        "session_id": "template",
                        "id": "template",
                        "base_instructions": "test instructions",
                        "cwd": "/wrong/template/cwd",
                        "history_mode": "paginated",
                        "history_base": {
                            "thread_id": "template",
                            "end_ordinal_exclusive": 34,
                            "end_byte_offset": 1234,
                        },
                        "forked_from_id": "template-parent",
                        "context_window": {"window_id": "template-window"},
                    },
                }) + "\n"
            )
            state_dir = kb_home / "example"
            state_dir.mkdir(parents=True)
            (state_dir / "state.json").write_text(json.dumps({
                "fed": ["pack-1", "pack-2"],
                "blobs": [
                    {"id": "blob-1", "type": "compaction", "encrypted_content": "one", "future_field": [1]},
                    {"id": "blob-2", "type": "compaction", "encrypted_content": "two"},
                ],
            }))

            env = dict(os.environ, HOME=str(home), KB_REPOMAP_HOME=str(kb_home))
            subprocess.run(
                [sys.executable, str(ROOT / "kb_fork_mint.py"), "--name", "example"],
                check=True,
                env=env,
                capture_output=True,
                text=True,
            )

            rollouts = list((home / ".codex" / "sessions").glob("*/*/*/rollout-*.jsonl"))
            self.assertEqual(len(rollouts), 2)
            minted = next(path for path in rollouts if path.name != "rollout-template.jsonl")
            items = [json.loads(line) for line in minted.read_text().splitlines()]
            self.assertEqual([item["ordinal"] for item in items], list(range(len(items))))
            self.assertEqual(items[0]["type"], "session_meta")
            self.assertEqual(items[0]["payload"]["cwd"], str(ROOT))
            self.assertEqual(items[0]["payload"]["history_mode"], "legacy")
            for inherited_key in ("history_base", "forked_from_id", "context_window"):
                self.assertNotIn(inherited_key, items[0]["payload"])
            self.assertEqual(
                [item["payload"]["type"] for item in items[1:-1]],
                ["compaction", "compaction"],
            )
            self.assertEqual(items[-1]["payload"]["type"], "message")
            self.assertEqual(items[-1]["payload"]["role"], "user")
            self.assertEqual(items[1]["payload"]["future_field"], [1])


if __name__ == "__main__":
    unittest.main()
