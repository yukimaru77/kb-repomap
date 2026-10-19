"""Machine-readable stored KB export without launching or rebuilding."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_cli
from kb_fork_mint import CHARTER


class ExportTest(unittest.TestCase):
    def test_export_preserves_items_and_appends_store_developer(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "v1.json"
            blob = {"type": "compaction", "encrypted_content": "opaque", "unknown": {"keep": True}}
            user = {"role": "user", "content": "Current repository map"}
            snapshot.write_text(json.dumps([blob, {"role": "user", "content": CHARTER}, user]))
            loaded = {"jsonl": snapshot, "developer_text": "保存済みの指示", "store": {"name": "research"},
                      "store_revision": "abc123", "info": {"source_commit": "def456"}}
            output = io.StringIO()
            with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                    mock.patch.object(kb_cli.store, "find_kb", return_value=loaded) as lookup, \
                    mock.patch.object(kb_cli, "launch") as launch, \
                    mock.patch.object(kb_cli, "create_kb") as rebuild, \
                    mock.patch.object(kb_cli.store, "source_update") as source_update, \
                    contextlib.redirect_stdout(output):
                # NAME=codex must not be mistaken for the native-client boundary.
                kb_cli.main(["export", "codex", "--store", "research", "--file", "v1"])
            lookup.assert_called_once_with({}, "codex", "research", filename="v1.json")
            launch.assert_not_called()
            rebuild.assert_not_called()
            source_update.assert_not_called()
            result = json.loads(output.getvalue())
            self.assertEqual(result, {
                "version": 1, "name": "codex", "store": "research", "store_revision": "abc123",
                "file": "v1.json", "info": {"source_commit": "def456"},
                "items": [blob, user, {"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "保存済みの指示"}]}],
            })

    def test_decrypted_export_emits_build_context_text(self):
        loaded = {"jsonl": Path("/store/example/v2.json"), "developer_text": "dev", "store": {"name": "research"},
                  "store_revision": "abc123", "info": {"source_commit": "def456"}}
        output = io.StringIO()
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
                mock.patch("kb_claude.build_context", return_value="## Decrypted KB material: a\n\nbody\n") as build, \
                mock.patch.object(kb_cli, "load_session_items") as items, \
                contextlib.redirect_stdout(output):
            kb_cli.main(["export", "example", "--store", "research", "--file", "v2", "--decrypted"])
        build.assert_called_once_with(loaded, "example", "v2.json")
        items.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {
            "version": 2, "kind": "decrypted", "name": "example", "store": "research",
            "store_revision": "abc123", "file": "v2.json", "info": {"source_commit": "def456"},
            "text": "## Decrypted KB material: a\n\nbody\n",
        })

    def test_decrypted_export_without_plaintext_emits_no_json(self):
        loaded = {"jsonl": Path("/store/example/latest.json"), "developer_text": None, "store": {"name": "first"},
                  "store_revision": "abc123", "info": {}}
        output = io.StringIO()
        with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
                mock.patch("kb_claude._decrypt_files", return_value=[]), \
                contextlib.redirect_stdout(output), self.assertRaisesRegex(ValueError, "復号済みKBがありません"):
            kb_cli.main(["export", "example", "--decrypted"])
        self.assertEqual(output.getvalue(), "")

    def test_invalid_snapshot_emits_no_partial_json(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "latest.jsonl"
            snapshot.write_text('[{"type":"compaction","encrypted_content":""}]')
            loaded = {"jsonl": snapshot, "developer_text": None, "store": {"name": "first"},
                      "store_revision": "abc123", "info": {}}
            output = io.StringIO()
            with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                    mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
                    contextlib.redirect_stdout(output), self.assertRaises(ValueError):
                kb_cli.main(["export", "example"])
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
