import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kb_claude
import kb_decrypt
import kb_meta_strip


def long_text(meta_lines=2, knowledge_lines=400):
    meta = [f"We must answer the user; output format rule {i}." for i in range(meta_lines)]
    knowledge = [f"gateway/src/module_{i}.ts#handler returns 409 when quota {i} exceeded" for i in range(knowledge_lines)]
    return "\n".join(meta + knowledge) + "\n"


class StripMetaTest(unittest.TestCase):
    def test_short_text_is_not_sent(self):
        caller = mock.Mock()
        clean, report = kb_meta_strip.strip_meta("short knowledge", caller=caller)
        caller.assert_not_called()
        self.assertEqual(clean, "short knowledge")
        self.assertEqual(report["status"], "short")

    def test_only_listed_lines_are_removed_verbatim(self):
        text = long_text()
        prompts = []
        def caller(prompt, model, effort):
            prompts.append(prompt)
            return 'reasoning... {"delete": [[1, 2]]}'
        clean, report = kb_meta_strip.strip_meta(text, caller=caller)
        self.assertIn("1\tWe must answer the user", prompts[0])
        self.assertEqual(report["status"], "stripped")
        self.assertEqual(report["deleted_lines"], [1, 2])
        self.assertNotIn("We must answer", clean)
        self.assertEqual(clean.splitlines(), text.splitlines()[2:])

    def test_large_deletion_is_rejected(self):
        text = long_text(knowledge_lines=300)
        clean, report = kb_meta_strip.strip_meta(text, caller=lambda *_: '{"delete": [[1, 250]]}')
        self.assertEqual(clean, text)
        self.assertTrue(report["status"].startswith("rejected"))

    def test_errors_and_invalid_ranges_keep_text(self):
        text = long_text()
        for answer in ("not json", '{"delete": [[0, 1], [5, 3], ["a", 2], [1, 99999]]}'):
            with self.subTest(answer=answer):
                clean, report = kb_meta_strip.strip_meta(text, caller=lambda *_: answer)
                self.assertEqual(clean, text)
        def failing(*_):
            raise OSError("connection reset")
        clean, report = kb_meta_strip.strip_meta(text, caller=failing)
        self.assertEqual(clean, text)
        self.assertIn("error", report["status"])


class KnowledgeFileTest(unittest.TestCase):
    def test_write_knowledge_creates_clean_copy_and_skips_unchanged_raw(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "01-blob"
            directory.mkdir()
            (directory / "raw.txt").write_text(long_text())
            calls = []
            def strip(text):
                calls.append(text)
                return text.split("\n", 2)[2], {"tokens": 1, "removed_tokens": 5, "deleted_lines": [1, 2],
                                                 "status": "stripped"}
            with mock.patch("kb_meta_strip.strip_meta", side_effect=strip):
                kb_decrypt.write_knowledge([directory])
                kb_decrypt.write_knowledge([directory])
            self.assertEqual(len(calls), 1)
            self.assertNotIn("We must answer", (directory / "knowledge.txt").read_text())
            report = json.loads((directory / "knowledge.json").read_text())
            self.assertEqual(report["status"], "stripped")
            self.assertIn("raw_sha256", report)
            self.assertEqual((directory / "raw.txt").read_text(), long_text())

    def test_claude_context_prefers_knowledge_text(self):
        listing = ["kb/decrypt/latest/01-a/raw.txt", "kb/decrypt/latest/01-a/knowledge.txt",
                   "kb/decrypt/latest/02-b/raw.txt"]
        contents = {"kb/decrypt/latest/01-a/raw.txt": "RAW A", "kb/decrypt/latest/01-a/knowledge.txt": "CLEAN A",
                    "kb/decrypt/latest/02-b/raw.txt": "RAW B"}
        def git(repo, *args):
            if args[0] == "ls-tree":
                return mock.Mock(stdout="\n".join(listing))
            return mock.Mock(stdout=contents[args[1].split(":", 1)[1]])
        with mock.patch.object(kb_claude.store, "sync_store", return_value=("repo", "rev", "main")), \
                mock.patch.object(kb_claude.store, "git", side_effect=git):
            files = kb_claude._decrypt_files({"store": {}}, "kb", "latest.json")
        self.assertEqual([text for _path, text in files], ["CLEAN A", "RAW B"])


if __name__ == "__main__":
    unittest.main()
