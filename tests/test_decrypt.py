import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_cli
import kb_decrypt
import kb_store


class SelectionTest(unittest.TestCase):
    def test_exact_token_count_wins_over_a_near_miss(self):
        candidates = [
            {"order": 0, "file": "near.txt", "tokens": 11},
            {"order": 1, "file": "exact.txt", "tokens": 10},
        ]
        chosen, reason = kb_decrypt.select_plaintext(candidates, 10)
        self.assertEqual(chosen["file"], "exact.txt")
        self.assertEqual(reason, kb_decrypt.THRESHOLD_REASON)

    def test_closest_token_count_is_used_when_nothing_matches(self):
        candidates = [
            {"order": 0, "file": "far.txt", "tokens": 20},
            {"order": 1, "file": "near.txt", "tokens": 12},
            {"order": 2, "file": "also-near.txt", "tokens": 9},
        ]
        chosen, reason = kb_decrypt.select_plaintext(candidates, 10)
        self.assertEqual(chosen["file"], "also-near.txt")
        self.assertEqual(reason, kb_decrypt.CLOSEST_REASON)

    def test_equal_distance_keeps_the_earlier_attempt(self):
        candidates = [
            {"order": 1, "file": "later.txt", "tokens": 12},
            {"order": 0, "file": "earlier.txt", "tokens": 8},
        ]
        chosen, _reason = kb_decrypt.select_plaintext(candidates, 10)
        self.assertEqual(chosen["file"], "earlier.txt")

    def test_two_percent_is_accepted_and_the_closer_text_wins(self):
        candidates = [
            {"order": 0, "file": "edge.txt", "tokens": 980},
            {"order": 1, "file": "outside.txt", "tokens": 979},
            {"order": 2, "file": "near.txt", "tokens": 1010},
        ]
        chosen, reason = kb_decrypt.select_plaintext(candidates, 1000)
        self.assertEqual((chosen["file"], reason), ("near.txt", kb_decrypt.THRESHOLD_REASON))

    def test_just_outside_two_percent_uses_the_closest_text(self):
        candidates = [
            {"order": 0, "file": "high.txt", "tokens": 1021},
            {"order": 1, "file": "low.txt", "tokens": 979},
        ]
        chosen, reason = kb_decrypt.select_plaintext(candidates, 1000)
        self.assertEqual((chosen["file"], reason), ("high.txt", kb_decrypt.CLOSEST_REASON))

    def test_stored_blob_tokens_override_live_attribution(self):
        tokens, source = kb_decrypt.target_blob_tokens(
            {"output_tokens": 4}, [{"blob_input_tokens": 10}])
        self.assertEqual((tokens, source), (4, "blob.output_tokens"))


class DecryptCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(kb_store, "CACHE", self.root / "cache").start()
        mock.patch.object(kb_store, "CONFIG", self.root / "config.json").start()
        mock.patch.dict(os.environ, {
            "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
        }).start()
        source = self.root / "source"
        kb_store.run(["git", "init", "-q", "-b", "main", str(source)])
        (source / "README.md").write_text("source")
        kb_store.git(source, "add", ".")
        kb_store.git(source, "commit", "-qm", "fixture")
        commit = kb_store.git(source, "rev-parse", "HEAD").stdout.strip()
        store = self.root / "store"
        kb_store.run(["git", "init", "-q", "-b", "main", str(store)])
        (store / "README.md").write_text("store")
        kb_store.git(store, "add", ".")
        kb_store.git(store, "commit", "-qm", "fixture")
        bare = self.root / "store.git"
        kb_store.run(["git", "clone", "--bare", str(store), str(bare)])
        self.store = {"name": "research", "url": str(bare)}
        snapshot = self.root / "kb.json"
        kb_decrypt.write_private(snapshot, json.dumps([{
            "type": "compaction", "id": "blob-a", "encrypted_content": "opaque"}]) + "\n")
        kb_store.publish(self.store, "example", {
            "repository_url": str(source), "source_commit": commit, "branch": "main"}, snapshot)
        self.calls = []
        self.lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with self.lock:
                    self.calls.append(body)
                    high = sum(item["model"] == "gpt-5.6-luna" and item["reasoning"]["effort"] == "high"
                               for item in self.calls)
                exact = high == 1 and body["model"] == "gpt-5.6-luna" and body["reasoning"]["effort"] == "high"
                text = "EXACT-PLAINTEXT" if exact else "OTHER"
                tokens = 10 if exact else 4
                message = {"type": "message", "id": "msg-1", "content": [{"type": "output_text", "text": text}]}
                usage = {"attribution": {"items": {
                    "blob-a": {"input_tokens": 10, "output_tokens": 0},
                    "msg-1": {"input_tokens": 0, "output_tokens": tokens},
                }}, "output_tokens": tokens}
                events = [
                    {"type": "response.output_text.delta", "delta": text},
                    {"type": "response.output_item.done", "item": message},
                    {"type": "response.completed", "response": {"output": [message], "usage": usage}},
                ]
                data = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        Handler.lock = self.lock
        Handler.calls = self.calls
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        key = self.root / "client.key"
        key.write_text("test-key")
        kb_store.write_config({"stores": [self.store], "build_args": []})
        self.env = mock.patch.dict(os.environ, {
            "KB_POOL_ORIGIN": f"http://127.0.0.1:{self.server.server_port}",
            "KB_POOL_KEY_FILE": str(key),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_later_waves_stop_once_a_blob_matches(self):
        kb_cli.main(["decrypt", "example"])
        self.assertEqual(len(self.calls), 6)
        pairs = [(body["model"], body["reasoning"]["effort"]) for body in self.calls]
        self.assertEqual(sorted(pairs[:4]), [
            ("gpt-6-luna", "high"), ("gpt-6-luna", "high"),
            ("gpt-6-luna", "max"), ("gpt-6-luna", "max")])
        self.assertEqual(sorted(pairs[4:]), [("gpt-5.6-luna", "high"), ("gpt-5.6-luna", "max")])
        for body in self.calls:
            self.assertEqual(body["input"][0]["id"], "blob-a")
            self.assertEqual(body["input"][1]["content"][0]["text"], kb_decrypt.PROMPT)
            self.assertEqual(body["tools"], [])
        shown = kb_store.git(self.store["url"], "ls-tree", "-r", "--name-only", "HEAD").stdout
        self.assertIn("example/decrypt/latest/01-blob-a/raw.txt", shown)
        self.assertIn("example/decrypt/latest/manifest.json", shown)
        raw = kb_store.git(self.store["url"], "show", "HEAD:example/decrypt/latest/01-blob-a/raw.txt").stdout
        self.assertEqual(raw, "EXACT-PLAINTEXT")
        selection = json.loads(kb_store.git(
            self.store["url"], "show", "HEAD:example/decrypt/latest/01-blob-a/selection.json").stdout)
        self.assertEqual(selection["reason"], kb_decrypt.THRESHOLD_REASON)
        self.assertEqual(selection["blob_tokens"], 10)
        self.assertEqual(selection["selected_tokens"], 10)
        self.assertEqual(selection["selected"], "05-gpt-5.6-luna-high-1.txt")
        before = len(self.calls)
        kb_cli.main(["decrypt", "example"])
        self.assertEqual(len(self.calls), before)


def attempt_record(blob_id, model, effort, repeat, tokens):
    return {
        "model": model, "effort": effort, "repeat": repeat,
        "file": kb_decrypt.attempt_filename(model, effort, repeat),
        "order": kb_decrypt.ATTEMPTS.index((model, effort, repeat)),
        "status": "complete", "tokens": tokens, "blob_input_tokens": 10,
        "answer": f"{blob_id}-{model}-{effort}-{repeat}",
    }


class ScheduleTest(unittest.TestCase):
    def jobs(self, names):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        made = []
        for name in names:
            directory = root / name
            directory.mkdir()
            made.append({"blob": {"id": name}, "directory": directory, "records": []})
        return made

    def test_matched_blob_drops_out_and_the_other_continues_one_wave(self):
        calls = []

        def runner(blob, directory, model, effort, repeat):
            calls.append((blob["id"], model, effort, repeat))
            tokens = 10 if blob["id"] == "a" or model == "gpt-5.6-luna" else 4
            return attempt_record(blob["id"], model, effort, repeat, tokens)

        kb_decrypt.run_schedule(self.jobs(("a", "b")), runner)
        self.assertEqual(sorted(call for call in calls if call[0] == "a"), [
            ("a", "gpt-6-luna", "high", 1), ("a", "gpt-6-luna", "max", 1)])
        self.assertEqual(sorted(call for call in calls if call[0] == "b"), sorted([
            ("b", "gpt-6-luna", "high", 1), ("b", "gpt-6-luna", "max", 1),
            ("b", "gpt-6-luna", "high", 2), ("b", "gpt-6-luna", "max", 2),
            ("b", "gpt-5.6-luna", "high", 1), ("b", "gpt-5.6-luna", "max", 1)]))

    def test_unmatched_blob_runs_the_four_waves_in_order(self):
        calls = []

        def runner(blob, directory, model, effort, repeat):
            calls.append((model, effort, repeat))
            return attempt_record(blob["id"], model, effort, repeat, 4)

        kb_decrypt.run_schedule(self.jobs(("only",)), runner)
        groups = [sorted(calls[index:index + 2]) for index in range(0, 8, 2)]
        self.assertEqual(groups, [sorted(wave) for wave in kb_decrypt.WAVES])

    def test_first_wave_runs_two_requests_for_every_blob_together(self):
        barrier = threading.Barrier(4)

        def runner(blob, directory, model, effort, repeat):
            barrier.wait(timeout=2)
            return attempt_record(blob["id"], model, effort, repeat, 10)

        kb_decrypt.run_schedule(self.jobs(("a", "b")), runner)
