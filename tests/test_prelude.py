import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_api


ROOT = Path(__file__).resolve().parents[1]


class PreludeOrderingTest(unittest.TestCase):
    def test_dry_run_places_prelude_before_source_packs(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repository = root / "repo"
            repository.mkdir()
            subprocess.run(["git", "init", "-q", str(repository)], check=True)
            (repository / "app.py").write_text("print('hello')\n")
            subprocess.run(["git", "-C", str(repository), "add", "app.py"], check=True)
            subprocess.run([
                "git", "-C", str(repository), "-c", "user.name=Test",
                "-c", "user.email=test@example.com", "commit", "-qm", "initial",
            ], check=True)
            repo_map = root / "repository-repo_map.md"
            repo_map.write_text("# Repository Overview\nTest repository.\n")
            prelude = root / "repository-hosting-metadata.md"
            prelude.write_text("# Repository Hosting Metadata\nAdmin: octocat\n")
            env = dict(os.environ)
            env["KB_REPOMAP_HOME"] = str(root / "state")
            result = subprocess.run([
                sys.executable, str(ROOT / "kb_repo.py"),
                "--repo", str(repository), "--repo-map", str(repo_map),
                "--prelude-file", str(prelude), "--name", "example", "--dry-run",
            ], check=True, capture_output=True, text=True, env=env)
            plan = json.loads(result.stdout)["plan"]
            self.assertEqual(plan[0]["kind"], "prelude")
            self.assertTrue(plan[0]["label"].startswith("prelude-01:"))
            self.assertTrue(plan[1]["label"].startswith("pack-01:"))

    def test_second_stage_keeps_prelude_first_and_does_not_recompress_it(self):
        prelude = {"id": "prelude", "type": "compaction", "encrypted_content": "p"}
        code1 = {"id": "code-1", "type": "compaction", "encrypted_content": "c1"}
        code2 = {"id": "code-2", "type": "compaction", "encrypted_content": "c2"}
        merged = {"id": "merged", "type": "compaction", "encrypted_content": "m"}
        state = {
            "blobs": [prelude, code1, code2],
            "base_items": [prelude, code1, code2],
            "prelude_blob_ids": ["prelude"],
            "stage2_ids": [],
            "rounds": [],
            "blob_output_tokens": {
                "prelude": 1000,
                "code-1": 2500,
                "code-2": 2500,
            },
        }
        with mock.patch.object(kb_api, "load_state", return_value=state), \
                mock.patch.object(kb_api, "save_state") as save_state, \
                mock.patch.object(kb_api, "state_path", return_value="/tmp/fake-state"), \
                mock.patch("shutil.copy"), \
                mock.patch.object(kb_api, "compact", return_value=([merged], 1.0, {"output_tokens": 3000})) as compact:
            kb_api.cmd_merge_old("example", keep_recent=0, budget_tokens=150_000)
        compact_items = compact.call_args.args[0]
        self.assertNotIn(prelude, compact_items)
        saved = save_state.call_args.args[1]
        self.assertEqual([item["id"] for item in saved["blobs"]], ["prelude", "merged"])
        self.assertEqual(saved["stage1_blob_count"], 3)
        self.assertEqual(saved["stage1_blob_output_tokens"], 6000)
        self.assertEqual(saved["blob_output_tokens"]["merged"], 3000)
        self.assertEqual(kb_api.current_blob_output_tokens(saved), 4000)

    def test_second_stage_compacts_independent_groups_in_parallel_and_keeps_order(self):
        blobs = [
            {
                "id": f"code-{index}",
                "type": "compaction",
                "encrypted_content": f"c{index}",
            }
            for index in range(6)
        ]
        state = {
            "blobs": blobs,
            "base_items": blobs,
            "prelude_blob_ids": [],
            "stage2_ids": [],
            "rounds": [],
            "blob_output_tokens": {blob["id"]: 2500 for blob in blobs},
        }
        both_workers_started = threading.Barrier(2)

        def compact_group(items, **_kwargs):
            both_workers_started.wait(timeout=2)
            first_id = items[1]["id"]
            merged = {
                "id": f"merged-{first_id}",
                "type": "compaction",
                "encrypted_content": f"m-{first_id}",
            }
            return [merged], 1.0, {"output_tokens": 3000}

        with mock.patch.object(kb_api, "load_state", return_value=state), \
                mock.patch.object(kb_api, "save_state") as save_state, \
                mock.patch.object(kb_api, "state_path", return_value="/tmp/fake-state"), \
                mock.patch("shutil.copy"), \
                mock.patch.object(
                    kb_api, "compact", side_effect=compact_group
                ) as compact:
            kb_api.cmd_merge_old(
                "example", keep_recent=0, budget_tokens=7500, workers=2
            )

        self.assertEqual(compact.call_count, 2)
        saved = save_state.call_args.args[1]
        self.assertEqual(
            [item["id"] for item in saved["blobs"]],
            ["merged-code-0", "merged-code-3"],
        )
        self.assertEqual(
            [round_["papers"] for round_ in saved["rounds"]],
            [["<merge raw 0..2>"], ["<merge raw 3..5>"]],
        )

    def test_second_stage_groups_by_measured_token_budget(self):
        blobs = [
            {
                "id": f"code-{index}",
                "type": "compaction",
                "encrypted_content": f"c{index}",
            }
            for index in range(5)
        ]
        measured = [70_000, 60_000, 30_000, 90_000, 10_000]
        state = {
            "blobs": blobs,
            "base_items": blobs,
            "prelude_blob_ids": [],
            "stage2_ids": [],
            "rounds": [],
            "blob_output_tokens": {
                blob["id"]: tokens for blob, tokens in zip(blobs, measured)
            },
        }

        def compact_group(items, **_kwargs):
            first_id = items[1]["id"]
            return [
                {
                    "id": f"merged-{first_id}",
                    "type": "compaction",
                    "encrypted_content": f"m-{first_id}",
                }
            ], 1.0, {"output_tokens": 3000}

        with mock.patch.object(kb_api, "load_state", return_value=state), \
                mock.patch.object(kb_api, "save_state") as save_state, \
                mock.patch.object(kb_api, "state_path", return_value="/tmp/fake-state"), \
                mock.patch("shutil.copy"), \
                mock.patch.object(kb_api, "compact", side_effect=compact_group) as compact:
            kb_api.cmd_merge_old(
                "example", keep_recent=0, budget_tokens=150_000, workers=1
            )

        self.assertEqual(compact.call_count, 2)
        self.assertEqual(
            [[item["id"] for item in call.args[0][1:]]
             for call in compact.call_args_list],
            [["code-0", "code-1"], ["code-2", "code-3", "code-4"]],
        )
        saved = save_state.call_args.args[1]
        self.assertEqual(
            [round_["input_blob_tokens"] for round_ in saved["rounds"]],
            [130_000, 130_000],
        )
        self.assertEqual(saved["stage2_blob_count"], 2)
        self.assertEqual(saved["stage2_blob_output_tokens"], 6000)

    def test_token_allocation_preserves_measured_total(self):
        state = {}
        blobs = [
            {"id": "short", "encrypted_content": "x"},
            {"id": "long", "encrypted_content": "y" * 9},
        ]
        kb_api.record_blob_output_tokens(state, blobs, {"output_tokens": 101})
        self.assertEqual(sum(state["blob_output_tokens"].values()), 101)
        self.assertGreater(
            state["blob_output_tokens"]["long"],
            state["blob_output_tokens"]["short"],
        )


if __name__ == "__main__":
    unittest.main()
