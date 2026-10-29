import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kb_claude
import kb_cli
import kb_codex_resume
from kb_items import CODEX_SUMMARY_PREFIX, KB_TEXT_MARKER, dump_items, load_session_items

BLOB_A = {"type": "compaction", "id": "cmp_a", "encrypted_content": "opaque-a"}
BLOB_B = {"type": "compaction", "id": "cmp_b", "encrypted_content": "opaque-b"}
MAP = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "MAP"}]}


def text_of(item):
    return item["content"][0]["text"]


class SessionItemsTest(unittest.TestCase):
    def snapshot(self, temp, items):
        path = Path(temp) / "kb.json"
        dump_items(path, items)
        return path

    def test_plaintext_replaces_blobs_with_one_codex_summary_user_message(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.snapshot(temp, [BLOB_A, BLOB_B, MAP])
            items = load_session_items(path, developer_text="DEV", kb_text="KNOWLEDGE A\n\nKNOWLEDGE B")
        self.assertEqual([item.get("type") for item in items], ["message", "message", "message"])
        self.assertEqual(items[0]["role"], "user")
        self.assertTrue(text_of(items[0]).startswith(CODEX_SUMMARY_PREFIX + "\n## Repository knowledge base (kb)\n"))
        self.assertIn("KNOWLEDGE A\n\nKNOWLEDGE B", text_of(items[0]))
        self.assertEqual(items[1], MAP)
        self.assertEqual((items[2]["role"], text_of(items[2])), ("developer", "DEV"))

    def test_without_plaintext_blobs_are_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.snapshot(temp, [BLOB_A, BLOB_B])
            self.assertEqual(load_session_items(path), [BLOB_A, BLOB_B])


class CodexKbTextTest(unittest.TestCase):
    def run_lookup(self, manifest, files):
        listing = [path for path in files]
        def git(repo, *args, check=True, text=True):
            if args[0] == "show" and args[1].endswith("manifest.json"):
                if manifest is None:
                    return mock.Mock(returncode=128, stdout="")
                return mock.Mock(returncode=0, stdout=json.dumps(manifest))
            if args[0] == "ls-tree":
                return mock.Mock(stdout="\n".join(listing))
            return mock.Mock(stdout=files[args[1].split(":", 1)[1]])
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp) / "kb.json"
            dump_items(snapshot, [BLOB_A, BLOB_B])
            with mock.patch.object(kb_claude.store, "sync_store", return_value=("repo", "rev", "main")), \
                    mock.patch.object(kb_claude.store, "git", side_effect=git):
                return kb_claude.codex_kb_text({"store": {}, "jsonl": snapshot}, "kb", "latest.json")

    FILES = {"kb/decrypt/latest/01-cmp_a/raw.txt": "RAW A", "kb/decrypt/latest/01-cmp_a/knowledge.txt": "CLEAN A",
             "kb/decrypt/latest/02-cmp_b/raw.txt": "RAW B"}

    def test_matching_manifest_returns_knowledge_text_in_blob_order(self):
        manifest = {"blobs": [{"blob_id": "cmp_a"}, {"blob_id": "cmp_b"}]}
        text = self.run_lookup(manifest, self.FILES)
        self.assertLess(text.index("CLEAN A"), text.index("RAW B"))
        self.assertNotIn("RAW A", text)
        self.assertIn("## KB section 1/2\n> " + kb_claude.SCOPE_NOTE + "\n\nCLEAN A\n\n## End of KB section 1/2", text)
        self.assertIn("## KB section 2/2", text)

    def test_section_titles_come_from_windows_when_counts_match(self):
        files = [("a/raw.txt", "A"), ("b/raw.txt", "B")]
        named = kb_claude.window_sections({"info": {"windows": [{"name": "Billing"}, {"name": "Keys"}]}}, files)
        self.assertTrue(named[0].startswith("## KB section 1/2: Billing\n"))
        self.assertTrue(named[1].startswith("## KB section 2/2: Keys\n"))
        plain = kb_claude.window_sections({"info": {"windows": [{"name": "only one"}]}}, files)
        self.assertTrue(plain[0].startswith("## KB section 1/2\n"))

    def test_stale_or_missing_plaintext_falls_back_to_blobs(self):
        self.assertIsNone(self.run_lookup({"blobs": [{"blob_id": "cmp_old"}, {"blob_id": "cmp_b"}]}, self.FILES))
        self.assertIsNone(self.run_lookup(None, self.FILES))

    def test_env_override_keeps_blobs(self):
        with mock.patch.dict(kb_cli.os.environ, {"KB_CODEX_KB": "blobs"}), \
                mock.patch("kb_claude.codex_kb_text") as lookup:
            self.assertIsNone(kb_cli.codex_plaintext({}, "kb", "latest.json"))
        lookup.assert_not_called()
        with mock.patch("kb_claude.codex_kb_text", side_effect=OSError("offline")):
            self.assertIsNone(kb_cli.codex_plaintext({}, "kb", "latest.json"))


class ResumeRecognitionTest(unittest.TestCase):
    def test_previous_kb_text_is_replaced_but_codex_summaries_are_kept(self):
        old_kb = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": KB_TEXT_MARKER + "OLD KNOWLEDGE\n"}]}
        new_kb = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": KB_TEXT_MARKER + "NEW KNOWLEDGE\n"}]}
        codex_summary = {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": CODEX_SUMMARY_PREFIX + "\nreal conversation summary"}]}
        question = {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}
        result = kb_codex_resume.convert([old_kb, question], [new_kb])
        self.assertEqual(result, [new_kb, question])
        kept = kb_codex_resume.convert([codex_summary, question], [new_kb])
        self.assertEqual(kept, [new_kb, codex_summary, question])


if __name__ == "__main__":
    unittest.main()
