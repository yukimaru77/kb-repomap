import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import kb_api
import kb_repo


class PipelineTest(unittest.TestCase):
    def test_default_then_count_grouping_over_http(self):
        self.check_pipeline(["--second-stage-count", "2"])

    def test_default_then_token_grouping_over_http(self):
        self.check_pipeline(["--second-stage-budget-tokens", "6000"])

    def test_reading_instructions_file_replaces_only_pack_instructions(self):
        self.check_pipeline(["--second-stage-count", "2"], reading_instructions="  検索と質問に使う知識を保持。\n\n名前も残す。\n")

    def test_api_instructions_file_applies_to_both_stages(self):
        self.check_pipeline(["--second-stage-count", "2"], instructions="  調査資料として保持。\n\n`literal` $HOME\n")

    def test_both_instruction_files_can_be_replaced(self):
        self.check_pipeline(["--second-stage-count", "2"],
                            reading_instructions="ファイル名と本文を覚えてください。\n",
                            instructions="検索用の知識を保持してください。\n")

    def test_empty_instruction_files_do_not_restore_defaults(self):
        self.check_pipeline(["--second-stage-count", "2"], reading_instructions="", instructions="")

    def check_pipeline(self, grouping_options, reading_instructions=None, instructions=None):
        calls = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    number = len(calls) + 1
                    calls.append((self.path, body))
                item = {"id": f"blob-{number}", "type": "compaction",
                        "encrypted_content": f"opaque-{number}", "future_field": [number]}
                events = [
                    {"type": "response.output_item.done", "item": item},
                    {"type": "response.completed", "response": {
                        "output": [], "usage": {"output_tokens": 3000}}},
                ]
                data = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                source = root / "source"
                source.mkdir()
                subprocess.run(["git", "init", "-q", str(source)], check=True)
                for index in range(4):
                    (source / f"module_{index}.py").write_text(
                        f"def marker_{index}():\n    return 'value_{index}'\n" +
                        (f"# Details of module {index}, keep this source material.\n" * 130)
                    )
                subprocess.run(["git", "-C", str(source), "add", "."], check=True)
                subprocess.run(["git", "-C", str(source), "-c", "user.name=Test",
                                "-c", "user.email=test@example.com", "commit", "-qm", "fixture"], check=True)
                key = root / "client.key"
                key.write_text("test-key")
                state_root = root / "state"
                env = dict(os.environ)
                env["KB_REPOMAP_HOME"] = str(state_root)
                command = [sys.executable, str(ROOT / "kb_repo_url.py"), str(source),
                           "--name", "fixture", "--budget-tokens", "5000", "--workers", "2",
                           "--origin", f"http://127.0.0.1:{server.server_port}",
                           "--key-file", str(key), "--no-mint"]
                for filename, option, value in (
                    ("reading instructions.txt", "--reading-instructions-file", reading_instructions),
                    ("api instructions.txt", "--instructions-file", instructions),
                ):
                    if value is not None:
                        path = root / filename
                        path.write_text(value, encoding="utf-8")
                        command += [option, str(path)]
                result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                state_path = state_root / "fixture" / "state.json"
                state = json.loads(state_path.read_text())
                expected_reading = kb_repo.READ_INSTRUCTION if reading_instructions is None else reading_instructions
                expected_api = kb_api.CHARTER if instructions is None else instructions
                self.assertEqual(state["reading_instructions"], expected_reading)
                self.assertEqual(state["instructions"], expected_api)
                self.assertGreater(state["stage1_blob_count"], 1)
                first_stage_count = len(state["blobs"])
                self.assertEqual(first_stage_count, state["stage1_blob_count"])
                self.assertNotIn("stage2_ids", state)
                self.assertTrue(all(
                    all(item.get("type") != "compaction" for item in body["input"])
                    for _, body in calls
                ))
                first_stage_calls = len(calls)
                repo_map = (state_root / "fixture" / "repository-map.txt").read_text()
                all_sources = []
                for _, body in calls:
                    text = body["input"][0]["content"][0]["text"]
                    block = text.split("===== READING INSTRUCTIONS =====\n\n", 1)[1]
                    actual_reading = block.split("\n\nこのblobに含まれるファイル:", 1)[0]
                    self.assertEqual(actual_reading, expected_reading)
                    self.assertIn(repo_map, text)
                    self.assertEqual(body["instructions"], expected_api)
                    all_sources.append(text)
                for index in range(4):
                    self.assertTrue(any((source / f"module_{index}.py").read_text() in text for text in all_sources))
                command += grouping_options
                merged = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(merged.returncode, 0, merged.stdout + merged.stderr)
                state = json.loads(state_path.read_text())
                self.assertEqual(len(state["blobs"]), (first_stage_count + 1) // 2)
                self.assertEqual(len(calls) - first_stage_calls, first_stage_count // 2)
                self.assertTrue(state["blobs"][0]["future_field"])
                self.assertIn("marker_0", (state_root / "fixture" / "repository-map.txt").read_text())
                self.assertTrue(all(path == "/_pool/rr/responses" for path, _ in calls))
                for _, body in calls:
                    self.assertEqual(body["instructions"], expected_api)
                    self.assertEqual(body["input"][-1], {"type": "compaction_trigger"})
                    self.assertEqual(body["model"], "gpt-5.6-sol")
                    self.assertEqual(body["reasoning"], {"effort": "high"})
                for _, body in calls[first_stage_calls:]:
                    self.assertEqual(sum(x.get("type") == "compaction" for x in body["input"]), 2)
                first_count = len(calls)
                again = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
                self.assertEqual(len(calls), first_count, "unchanged packs must not be regenerated")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
