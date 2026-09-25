import itertools
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kb_api
import kb_cli
import kb_repo
from kb_incremental import manifest_from_state, reuse_input
from kb_items import dump_items, load_items


class IncrementalBuildTest(unittest.TestCase):
    def test_cli_passes_previous_packs_and_attaches_current_map(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blob = {"type": "compaction", "id": "prior", "encrypted_content": "opaque"}
            snapshot = root / "prior.json"
            dump_items(snapshot, [blob])
            previous = {"info": {"source_commit": "a" * 40, "pack_manifest": {
                "format": 1,
                "reading_sha256": hashlib.sha256(kb_repo.READ_INSTRUCTION.encode()).hexdigest(),
                "instructions_sha256": hashlib.sha256(kb_api.CHARTER.encode()).hexdigest(),
                "packs": [{"files": ["a.py"], "blob_ids": ["prior"]}]}},
                "jsonl": snapshot}
            current_map = root / "map.txt"
            current_map.write_text("current map content")
            commands = []
            def run(command, *, env, check):
                commands.append(command)
                state = Path(env["KB_REPOMAP_HOME"]) / "fixture/state.json"
                state.parent.mkdir(parents=True)
                state.write_text(json.dumps({"blobs": [blob], "reused_packs": 1,
                                             "repo_map": str(current_map)}))
            with mock.patch.object(kb_cli, "BUILD_ROOT", root / "builds"), \
                    mock.patch.object(kb_cli.subprocess, "run", side_effect=run):
                output = kb_cli.rebuild("fixture", {"repository_url": "local"}, "b" * 40,
                                        {"build_args": []}, previous)
            self.assertIn("--reuse-manifest", commands[0])
            items = load_items(output)
            self.assertEqual(items[0], blob)
            self.assertIn("current map content", items[1]["content"][0]["text"])

    def test_changed_pack_only_is_compacted_and_manifest_can_be_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            for index in range(4):
                (source / f"module_{index}.py").write_text(
                    f"def marker_{index}():\n    return {index}\n" +
                    (f"# Long source details for module {index}.\n" * 150))
            def commit():
                subprocess.run(["git", "-C", str(source), "add", "."], check=True)
                subprocess.run(["git", "-C", str(source), "-c", "user.name=Test",
                                "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)
                return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            base = commit()
            repo_map = root / "map.txt"
            repo_map.write_text("Old repository map\n")
            sequence = itertools.count(1)
            calls = []
            def compact(items, **_kwargs):
                calls.append(items[0]["content"][0]["text"])
                number = next(sequence)
                return ([{"type": "compaction", "id": f"blob-{number}",
                          "encrypted_content": f"opaque-{number}"}], 0, {"output_tokens": 3000})
            def build(state_root, reuse=None):
                argv = ["kb_repo.py", "--repo", str(source), "--repo-map", str(repo_map),
                        "--name", "fixture", "--budget-tokens", "3800", "--workers", "1",
                        "--thin-threshold", "0"]
                if reuse:
                    argv += ["--reuse-manifest", str(reuse)]
                with mock.patch.object(kb_api, "STATE_ROOT", str(state_root)), \
                        mock.patch.object(kb_api, "compact", side_effect=compact), \
                        mock.patch.object(sys, "argv", argv):
                    kb_repo.main()
                return json.loads((state_root / "fixture/state.json").read_text())
            first = build(root / "first")
            self.assertGreater(len(first["blobs"]), 1)
            self.assertNotIn(kb_repo.READ_INSTRUCTION, json.dumps(manifest_from_state(first)))
            snapshot = root / "old.json"
            dump_items(snapshot, first["blobs"])
            loaded = {"info": {"source_commit": base, "pack_manifest": manifest_from_state(first)},
                      "jsonl": snapshot}
            previous = reuse_input(loaded, "fixture", root / "second" / "fixture" / "new")
            self.assertIsNotNone(previous)
            legacy = {"info": {"source_commit": base}, "jsonl": snapshot}
            self.assertEqual(len(reuse_input(legacy, "fixture", root / "new-build")["packs"]),
                             len(first["blobs"]))
            reuse = root / "reuse.json"
            reuse.write_text(json.dumps(previous))
            (source / "module_0.py").write_text((source / "module_0.py").read_text() + "# changed\n")
            second_commit = commit()
            repo_map.write_text("New repository map\n")
            before = len(calls)
            second = build(root / "second", reuse)
            self.assertEqual(len(calls) - before, 1)
            self.assertEqual(second["reused_packs"], len(first["blobs"]) - 1)
            old_ids = {item["id"] for item in first["blobs"]}
            new_ids = {item["id"] for item in second["blobs"]}
            self.assertEqual(len(old_ids & new_ids), len(old_ids) - 1)
            self.assertIn("New repository map", calls[-1])
            self.assertEqual(len(manifest_from_state(second)["packs"]), len(second["blobs"]))

            snapshot2 = root / "second.json"
            dump_items(snapshot2, second["blobs"])
            loaded2 = {"info": {"source_commit": second_commit,
                                "pack_manifest": manifest_from_state(second)}, "jsonl": snapshot2}
            reuse2 = root / "reuse2.json"
            reuse2.write_text(json.dumps(reuse_input(loaded2, "fixture", root / "third")))
            (source / "module_1.py").unlink()
            (source / "module_4.py").write_text("# New module\n" * 150)
            third_commit = commit()
            before = len(calls)
            third = build(root / "third", reuse2)
            self.assertEqual(len(calls) - before, 1)
            self.assertEqual(third["reused_packs"], 3)
            self.assertNotIn("blob-2", {item["id"] for item in third["blobs"]})

            snapshot3 = root / "third.json"
            dump_items(snapshot3, third["blobs"])
            loaded3 = {"info": {"source_commit": third_commit,
                                "pack_manifest": manifest_from_state(third)}, "jsonl": snapshot3}
            reuse3 = root / "reuse3.json"
            reuse3.write_text(json.dumps(reuse_input(loaded3, "fixture", root / "fourth")))
            repo_map.write_text("Only the repository map changed\n")
            before = len(calls)
            fourth = build(root / "fourth", reuse3)
            self.assertEqual(len(calls), before)
            self.assertEqual(fourth["reused_packs"], len(third["blobs"]))

            incompatible = json.loads(reuse3.read_text())
            incompatible["reading_sha256"] = "different"
            reuse3.write_text(json.dumps(incompatible))
            before = len(calls)
            fifth = build(root / "fifth", reuse3)
            self.assertEqual(len(calls) - before, len(third["blobs"]))
            self.assertFalse(fifth.get("reused_packs"))


if __name__ == "__main__":
    unittest.main()
