import contextlib
import io
import json
import unittest
from unittest import mock

import test_kb_cli
import kb_cli
import kb_store
from kb_items import load_items


class PaperStoreTest(unittest.TestCase):
    setUp = test_kb_cli.GitStoreTest.setUp
    repo = test_kb_cli.GitStoreTest.repo
    commit = test_kb_cli.GitStoreTest.commit

    def fixture(self):
        run = self.root / "paper-run"
        run.mkdir()
        (run / "kb.json").write_text(json.dumps(self.blobs[:3]))
        (run / "manifest.json").write_text(json.dumps({"source_sha256": "a" * 64,
            "source_git": {"repository_url": str(self.source), "source_commit": self.base, "branch": "main", "subdir": "."},
            "model": "gpt-6-astra", "effort": "low", "budget": 150000,
            "accounts": ["private-account"], "key_file": "private-key-path"}))
        (run / "result.json").write_text(json.dumps({"references": 2, "main_blobs": 1, "items": 3}))
        kb_store.write_config(self.config)
        return run

    def test_paper_publish_list_launch_checks_git_without_code_rebuild(self):
        run = self.fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["publish-paper", "paper", "--run", str(run), "--store", "second"])
        loaded = kb_store.find_kb(self.config, "paper", "second")
        self.assertEqual(load_items(loaded["jsonl"]), self.blobs[:3])
        self.assertEqual(loaded["info"]["source_kind"], "paper")
        self.assertEqual(loaded["info"]["references"], 2)
        self.assertEqual(loaded["info"]["reference_blobs"], 2)
        self.assertNotIn("accounts", json.dumps(loaded["info"]))
        self.assertNotIn("private-key-path", json.dumps(loaded["info"]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(kb_store, "source_update") as update, \
             mock.patch.object(kb_cli.kb_codex, "start_session", return_value="paper-id") as start:
            kb_cli.main(["list", "--store", "second"])
            kb_cli.main(["codex", "paper", "--store", "second", "--session-only"])
        self.assertIn(f"paper\tsecond\t{self.base[:12]}\tpaper\tlatest.json", output.getvalue())
        self.assertIn("元Gitに更新があります", output.getvalue())
        self.assertIn("引用資料 2件 / 引用blob 2個 / 本論文blob 1個", output.getvalue())
        update.assert_not_called()
        self.assertEqual(start.call_args.args[3], "")
        with mock.patch.object(kb_store, "source_head") as head, self.assertRaisesRegex(ValueError, "paper-kb"):
            kb_cli.main(["create", "paper", "--store", "second"])
        head.assert_not_called()

    def test_reduced_references_publish_and_launch_keep_material_and_blob_counts(self):
        run = self.fixture()
        (run / "kb.json").write_text(json.dumps(self.blobs[:9]))
        (run / "result.json").write_text(json.dumps({
            "references": 94, "reference_blobs": 8, "main_blobs": 1, "items": 9}))
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["publish-paper", "paper", "--run", str(run), "--store", "second"])
        loaded = kb_store.find_kb(self.config, "paper", "second")
        self.assertEqual(load_items(loaded["jsonl"]), self.blobs[:9])
        self.assertEqual(loaded["info"]["references"], 94)
        self.assertEqual(loaded["info"]["reference_blobs"], 8)
        self.assertEqual(loaded["info"]["main_blobs"], 1)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             mock.patch.object(kb_cli.kb_codex, "start_session", return_value="paper-id"):
            kb_cli.main(["codex", "paper", "--store", "second", "--session-only"])
        self.assertIn("引用資料 94件 / 引用blob 8個 / 本論文blob 1個", output.getvalue())

        (run / "result.json").write_text(json.dumps({
            "references": 94, "reference_blobs": 7, "main_blobs": 1, "items": 9}))
        with mock.patch.object(kb_store, "publish") as publish, self.assertRaisesRegex(ValueError, "blob数"):
            kb_cli.main(["publish-paper", "paper", "--run", str(run), "--store", "second"])
        publish.assert_not_called()

    def test_named_paper_leaves_latest_unbuilt(self):
        run = self.fixture()
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(["publish-paper", "paper", "--run", str(run), "--store", "second", "--file", "v1"])
        named = kb_store.find_kb(self.config, "paper", "second", filename="v1.json")
        self.assertEqual(load_items(named["jsonl"]), self.blobs[:3])
        with self.assertRaisesRegex(ValueError, "未作成"):
            kb_store.find_kb(self.config, "paper", "second")

    def test_paper_publish_includes_run_developer_and_preserves_it_when_missing(self):
        run = self.fixture()
        developer = ".\n└── translation/ (Translated paper)\n"
        (run / "dev.txt").write_text(developer, encoding="utf-8")
        command = ["publish-paper", "paper", "--run", str(run), "--store", "second"]
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(command)
        loaded = kb_store.find_kb(self.config, "paper", "second")
        self.assertEqual(loaded["developer_text"], developer)
        self.assertEqual(load_items(loaded["jsonl"]), self.blobs[:3])
        (run / "dev.txt").unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            kb_cli.main(command)
        repeated = kb_store.find_kb(self.config, "paper", "second")
        self.assertEqual(repeated["developer_text"], developer)
        self.assertEqual(repeated["store_revision"], loaded["store_revision"])

    def test_incomplete_paper_is_not_published(self):
        run = self.fixture()
        (run / "kb.json").write_text(json.dumps(self.blobs[:1]))
        with mock.patch.object(kb_store, "publish") as publish, self.assertRaisesRegex(ValueError, "blob数"):
            kb_cli.main(["publish-paper", "paper", "--run", str(run)])
        publish.assert_not_called()

    def test_paper_requires_git_provenance(self):
        run = self.fixture()
        manifest = json.loads((run / "manifest.json").read_text())
        del manifest["source_git"]
        (run / "manifest.json").write_text(json.dumps(manifest))
        with mock.patch.object(kb_store, "publish") as publish, self.assertRaisesRegex(ValueError, "元Git"):
            kb_cli.main(["publish-paper", "paper", "--run", str(run)])
        publish.assert_not_called()
