import itertools
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
import kb_partition
import kb_repo
from kb_incremental import manifest_from_state, reuse_input
from kb_items import dump_items, load_items


class PartitionRepairTest(unittest.TestCase):
    def test_repair_dedupes_assigns_missing_by_directory_and_splits_over_cap(self):
        files = ["ui/billing/page.tsx", "ui/billing/form.tsx", "api/billing.ts", "api/keys.ts",
                 "ui/keys/page.tsx", "db/0001.sql"]
        tokens = {path: 10 for path in files}
        tokens["db/0001.sql"] = 30
        windows = [
            {"name": "billing", "rationale": "charge flow", "files": ["ui/billing/page.tsx", "api/billing.ts"]},
            {"name": "keys", "rationale": "key flow", "files": ["api/keys.ts", "api/billing.ts", "gone.ts"]},
        ]
        fixed = kb_partition.repair(windows, files, tokens, cap=40)
        layout = {window["name"]: window["files"] for window in fixed}
        # duplicate api/billing.ts stays in its first window; unknown gone.ts is dropped
        self.assertIn("api/billing.ts", layout["billing"])
        self.assertNotIn("api/billing.ts", layout["keys"])
        # members follow repository file order
        self.assertEqual(layout["billing"], sorted(layout["billing"], key=files.index))
        # ui/billing/form.tsx joins the window sharing its directory
        self.assertIn("ui/billing/form.tsx", layout["billing"])
        self.assertEqual(sorted(path for window in fixed for path in window["files"]), sorted(files))
        self.assertTrue(all(sum(tokens[path] for path in window["files"]) <= 40 or len(window["files"]) == 1
                            for window in fixed))

    def test_repair_splits_window_in_file_order(self):
        files = ["a/1.ts", "a/2.ts", "a/3.ts"]
        tokens = {path: 50 for path in files}
        fixed = kb_partition.repair([{"name": "a", "rationale": "", "files": files}], files, tokens, cap=100)
        self.assertEqual([window["name"] for window in fixed], ["a (part 1)", "a (part 2)"])
        self.assertEqual(fixed[0]["files"], ["a/1.ts", "a/2.ts"])

    def test_design_retries_unparseable_output_and_repairs(self):
        files = ["ui/x.tsx", "api/x.ts", "docs/x.md"]
        tokens = {path: 5 for path in files}
        texts = {"ui/x.tsx": "export function X() {}", "api/x.ts": "test('x works', () => {})",
                 "docs/x.md": "# Guide\n"}
        replies = iter(["not json", json.dumps({"windows": [{"name": "x", "rationale": "feature x",
                                                             "files": [0, 1]}]})])
        prompts = []
        def caller(prompt, model, effort):
            prompts.append(prompt)
            return next(replies)
        with mock.patch.object(kb_partition.time, "sleep"):
            windows = kb_partition.design(files, tokens, texts, count=2, cap=100, caller=caller)
        self.assertEqual(len(prompts), 2)
        self.assertIn("exports X", prompts[0])
        self.assertIn("No previous knowledge", prompts[0])
        self.assertEqual(windows[0]["files"], ["ui/x.tsx", "api/x.ts", "docs/x.md"])

    def test_design_uses_knowledge_instead_of_index(self):
        files = ["a.ts"]
        seen = []
        def caller(prompt, model, effort):
            seen.append(prompt)
            return json.dumps({"windows": [{"name": "a", "files": [0]}]})
        kb_partition.design(files, {"a.ts": 1}, {"a.ts": "export const A = 1"}, knowledge="KB TEXT",
                            caller=caller)
        self.assertIn("KB TEXT", seen[0])
        self.assertNotIn("exports A", seen[0])


class WindowBuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for directory in ("ui/billing", "api", "ui/keys"):
            (self.source / directory).mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        files = {"ui/billing/page.tsx": "billing page", "api/billing.ts": "billing api",
                 "api/keys.ts": "keys api", "ui/keys/page.tsx": "keys page"}
        for path, marker in files.items():
            (self.source / path).write_text(f"// {marker}\n" + f"// {marker} details\n" * 40)
        self.commit()
        self.repo_map = self.root / "map.txt"
        self.repo_map.write_text("REPOSITORY MAP MARKER\n")
        self.windows = self.root / "windows.json"
        self.windows.write_text(json.dumps([
            {"name": "billing", "rationale": "charge flow", "files": ["ui/billing/page.tsx", "api/billing.ts"]},
            {"name": "keys", "rationale": "key flow", "files": ["api/keys.ts", "ui/keys/page.tsx"]},
        ]))
        self.reading = self.root / "reading.txt"
        self.reading.write_text("WINDOW={NAME}; WHY={RATIONALE}\n")
        self.calls = []
        self.sequence = itertools.count(1)

    def tearDown(self):
        self.temp.cleanup()

    def commit(self):
        subprocess.run(["git", "-C", str(self.source), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(self.source), "-c", "user.name=Test", "-c", "user.email=t@example.com",
                        "commit", "-qm", "fixture"], check=True)
        return subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()

    def compact(self, items, **_kwargs):
        self.calls.append(items[0]["content"][0]["text"])
        number = next(self.sequence)
        return ([{"type": "compaction", "id": f"blob-{number}", "encrypted_content": f"opaque-{number}"}],
                0, {"output_tokens": 3000})

    def build(self, state_root, reuse=None, extra=()):
        argv = ["kb_repo.py", "--repo", str(self.source), "--repo-map", str(self.repo_map),
                "--name", "fixture", "--workers", "1", "--thin-threshold", "0",
                "--windows", str(self.windows), "--reading-instructions-file", str(self.reading), *extra]
        if reuse:
            argv += ["--reuse-manifest", str(reuse)]
        with mock.patch.object(kb_api, "STATE_ROOT", str(state_root)), \
                mock.patch.object(kb_api, "compact", side_effect=self.compact), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(kb_repo, "repomix_candidates",
                                  side_effect=lambda repo: set(kb_repo.tracked_files(repo))):
            kb_repo.main()
        return json.loads((state_root / "fixture/state.json").read_text())

    def test_windows_are_packs_with_named_instructions_and_no_repository_map(self):
        state = self.build(self.root / "first")
        self.assertEqual(len(self.calls), 2)
        billing = next(call for call in self.calls if "WINDOW=billing" in call)
        self.assertIn("WHY=charge flow", billing)
        self.assertIn("ui/billing/page.tsx", billing)
        self.assertIn("api/billing.ts", billing)
        self.assertNotIn("api/keys.ts", billing)
        self.assertTrue(all("REPOSITORY MAP MARKER" not in call for call in self.calls))
        self.assertEqual([window["name"] for window in state["windows"]], ["billing", "keys"])
        self.assertEqual([round_["source"] for round_ in sorted(state["rounds"], key=lambda r: r["plan_order"])],
                         ["pack-01:billing", "pack-02:keys"])
        self.assertIsNotNone(manifest_from_state(state))

    def test_unchanged_windows_are_reused_and_new_files_join_closest_window(self):
        first = self.build(self.root / "first")
        base = subprocess.check_output(["git", "-C", str(self.source), "rev-parse", "HEAD"], text=True).strip()
        snapshot = self.root / "first.json"
        dump_items(snapshot, first["blobs"])
        loaded = {"info": {"source_commit": base, "pack_manifest": manifest_from_state(first)}, "jsonl": snapshot}
        reuse = self.root / "reuse.json"
        reuse.write_text(json.dumps(reuse_input(loaded, "fixture", self.root / "second")))
        (self.source / "ui/keys/detail.tsx").write_text("// keys detail\n" * 30)
        self.commit()
        before = len(self.calls)
        second = self.build(self.root / "second", reuse)
        self.assertEqual(len(self.calls) - before, 1)
        self.assertIn("WINDOW=keys", self.calls[-1])
        self.assertIn("ui/keys/detail.tsx", self.calls[-1])
        self.assertEqual(second["reused_packs"], 1)
        self.assertIn("ui/keys/detail.tsx", second["windows"][1]["files"])

    def test_missing_windows_are_designed_once_and_written(self):
        self.windows.unlink()
        designed = [{"name": "all", "rationale": "everything",
                     "files": ["ui/billing/page.tsx", "api/billing.ts", "api/keys.ts", "ui/keys/page.tsx"]}]
        with mock.patch("kb_partition.design", return_value=designed) as design:
            state = self.build(self.root / "first", extra=("--design-windows", "3", "--windows-cap", "100"))
        self.assertEqual(design.call_args.kwargs["count"], 3)
        self.assertEqual(design.call_args.kwargs["cap"], 100)
        self.assertEqual(json.loads(self.windows.read_text()), designed)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(state["windows"][0]["name"], "all")

    def test_small_repository_is_one_window_without_calling_the_designer(self):
        self.windows.unlink()
        with mock.patch("kb_partition.design") as design:
            state = self.build(self.root / "first", extra=("--design-windows", "3"))
        design.assert_not_called()
        self.assertEqual([window["name"] for window in state["windows"]], ["repository"])
        self.assertEqual(len(self.calls), 1)

    def test_failed_design_falls_back_to_directory_packs(self):
        self.windows.unlink()
        with mock.patch("kb_partition.design", side_effect=RuntimeError("window design failed: boom")):
            state = self.build(self.root / "first", extra=("--design-windows", "3", "--windows-cap", "100"))
        self.assertNotIn("windows", state)
        self.assertFalse(self.windows.exists())
        self.assertTrue(self.calls)
        self.assertTrue(all("REPOSITORY MAP MARKER" in call for call in self.calls))


class WindowCliTest(unittest.TestCase):
    def test_rebuild_passes_stored_windows_and_skips_repository_map_attachment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            blob = {"type": "compaction", "id": "w1", "encrypted_content": "opaque"}
            commands = []
            stored = [{"name": "billing", "rationale": "", "files": ["a.ts"]}]
            def run(command, *, env, check):
                commands.append(command)
                windows_file = Path(command[command.index("--windows") + 1])
                self.assertEqual(json.loads(windows_file.read_text()), stored)
                state = Path(env["KB_REPOMAP_HOME"]) / "fixture/state.json"
                state.parent.mkdir(parents=True)
                state.write_text(json.dumps({"blobs": [blob], "reused_packs": 1, "windows": stored,
                                             "repo_map": str(root / "missing-map.txt")}))
            with mock.patch.object(kb_cli, "BUILD_ROOT", root / "builds"), \
                    mock.patch.object(kb_cli.subprocess, "run", side_effect=run):
                output = kb_cli.rebuild("fixture", {"repository_url": "local", "windows": stored}, "b" * 40,
                                        {"build_args": []}, knowledge="ignored when windows are stored")
            self.assertNotIn("--windows-knowledge", commands[0])
            items = load_items(output)
            self.assertEqual(items[0], blob)
            self.assertEqual(len(items), 2)  # blob + charter, no repository map item

    def test_rebuild_without_windows_passes_knowledge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            commands = []
            def run(command, *, env, check):
                commands.append(command)
                knowledge = Path(command[command.index("--windows-knowledge") + 1])
                self.assertEqual(knowledge.read_text(), "PRIOR KB")
                state = Path(env["KB_REPOMAP_HOME"]) / "fixture/state.json"
                state.parent.mkdir(parents=True)
                state.write_text(json.dumps({"blobs": [{"type": "compaction", "id": "x", "encrypted_content": "o"}]}))
            with mock.patch.object(kb_cli, "BUILD_ROOT", root / "builds"), \
                    mock.patch.object(kb_cli.subprocess, "run", side_effect=run):
                kb_cli.rebuild("fixture", {"repository_url": "local"}, "c" * 40, {"build_args": []},
                               knowledge="PRIOR KB")
            self.assertIn("--windows", commands[0])

    def test_create_kb_stores_built_windows_and_repartition_drops_them(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            snapshot = root / "fixture" / "kb.json"
            snapshot.parent.mkdir(parents=True)
            dump_items(snapshot, [{"type": "compaction", "id": "x", "encrypted_content": "o"}])
            built = [{"name": "new", "rationale": "", "files": ["a.ts"]}]
            (snapshot.parent / "state.json").write_text(json.dumps({"windows": built}))
            published = {}
            seen_info = {}
            def rebuild(name, info, commit, config, previous=None, knowledge=None):
                seen_info.update(info)
                return snapshot
            def publish(store_entry, name, info, jsonl, filename, default_developer_text):
                published.update(info)
                return "rev"
            loaded = {"store": {"name": "s"}, "info": {"repository_url": "local",
                                                     "windows": [{"name": "old", "files": ["a.ts"]}]}}
            with mock.patch.object(kb_cli, "rebuild", side_effect=rebuild), \
                    mock.patch.object(kb_cli.store, "publish", side_effect=publish), \
                    mock.patch.object(kb_cli, "default_developer_text", return_value=""), \
                    mock.patch.object(kb_cli, "window_knowledge", return_value="KB"):
                kb_cli.create_kb("fixture", loaded, "d" * 40, {}, repartition=True)
            self.assertNotIn("windows", seen_info)
            self.assertEqual(published["windows"], built)


    def test_create_cli_seeds_windows_from_file(self):
        with tempfile.TemporaryDirectory() as temp:
            layout = Path(temp) / "layout.json"
            layout.write_text(json.dumps([{"name": "billing", "rationale": "", "files": ["a.ts"]}]))
            loaded = {"store": {"name": "s"}, "info": {"repository_url": "local"}}
            with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                    mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
                    mock.patch.object(kb_cli.store, "source_head", return_value="e" * 40), \
                    mock.patch.object(kb_cli, "previous_kb", return_value=None), \
                    mock.patch.object(kb_cli, "create_kb") as create, \
                    mock.patch.object(kb_cli, "decrypt_after_publish"):
                kb_cli.main(["create", "fixture", "--windows", str(layout), "--no-decrypt"])
            self.assertEqual(create.call_args.args[1]["info"]["windows"][0]["name"], "billing")
            bad = Path(temp) / "bad.json"
            bad.write_text(json.dumps({"windows": []}))
            with mock.patch.object(kb_cli.store, "read_config", return_value={}), \
                    mock.patch.object(kb_cli.store, "find_kb", return_value=loaded), \
                    self.assertRaises(ValueError):
                kb_cli.main(["create", "fixture", "--windows", str(bad)])


class ResumeTest(unittest.TestCase):
    def test_unfinished_build_picks_latest_checkpoint_without_kb_json(self):
        with tempfile.TemporaryDirectory() as temp:
            builds = Path(temp)
            def make(label, commit, finished, mtime):
                state = builds / "fixture" / label / "fixture" / "state.json"
                state.parent.mkdir(parents=True)
                state.write_text(json.dumps({"commit": commit, "blobs": [{"id": "x"}]}))
                if finished:
                    (state.parent / "kb.json").write_text("[]")
                import os
                os.utime(state, (mtime, mtime))
            make("old", "a" * 40, False, 100)
            make("done", "b" * 40, True, 300)
            make("new", "c" * 40, False, 200)
            with mock.patch.object(kb_cli, "BUILD_ROOT", builds):
                found = kb_cli.unfinished_build("fixture")
                self.assertEqual(found["root"].name, "new")
                self.assertEqual(found["commit"], "c" * 40)
                self.assertIsNone(kb_cli.unfinished_build("other"))

    def test_resume_reuses_build_root_and_commit_and_workers_override(self):
        with tempfile.TemporaryDirectory() as temp:
            builds = Path(temp) / "builds"
            partial = builds / "fixture" / "partial"
            (partial / "fixture").mkdir(parents=True)
            (partial / "fixture" / "state.json").write_text(json.dumps({"commit": "f" * 40, "blobs": []}))
            commands = []
            def run(command, *, env, check):
                commands.append((command, env))
                (Path(env["KB_REPOMAP_HOME"]) / "fixture" / "state.json").write_text(json.dumps(
                    {"commit": "f" * 40, "blobs": [{"type": "compaction", "id": "x", "encrypted_content": "o"}]}))
            published = {}
            loaded = {"store": {"name": "s"}, "info": {"repository_url": "local",
                                                     "windows": [{"name": "w", "files": ["a.ts"]}]}}
            with mock.patch.object(kb_cli, "BUILD_ROOT", builds), \
                    mock.patch.object(kb_cli.subprocess, "run", side_effect=run), \
                    mock.patch.object(kb_cli.store, "publish", side_effect=lambda *a, **k: published.update(a[2]) or "r"), \
                    mock.patch.object(kb_cli, "default_developer_text", return_value=""):
                kb_cli.create_kb("fixture", loaded, "0" * 40, {"build_args": ["--workers", "12"]},
                                 resume=True, workers=3)
            command, env = commands[0]
            self.assertEqual(env["KB_REPOMAP_HOME"], str(partial))
            self.assertEqual(command[command.index("--ref") + 1], "f" * 40)
            self.assertEqual(command[len(command) - 1 - command[::-1].index("--workers") + 1], "3")
            self.assertEqual(published["source_commit"], "f" * 40)
            with mock.patch.object(kb_cli, "BUILD_ROOT", Path(temp) / "empty"), self.assertRaises(ValueError):
                kb_cli.create_kb("fixture", loaded, "0" * 40, {}, resume=True)


if __name__ == "__main__":
    unittest.main()
