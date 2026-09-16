import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_api
import kb_repo


def sse(*events):
    return io.BytesIO(b"".join(
        ("data: " + json.dumps(event) + "\n\n").encode() for event in events
    ))


def success(item=None, output_tokens=3000):
    if item is None:
        item = {"type": "compaction", "id": "blob", "encrypted_content": "opaque",
                "future_metadata": {"keep": True}}
    return sse(
        {"type": "response.compaction.compacting"},
        {"type": "future.event", "unknown": True},
        {"type": "response.output_item.done", "item": item},
        {"type": "response.completed", "response": {
            "output": [], "usage": {"input_tokens": 5000, "output_tokens": output_tokens}}},
    )


def http_error(code, headers=None):
    return urllib.error.HTTPError("https://pool.invalid/_pool/rr/responses", code,
                                  "failure", headers or {}, None)


class CompactV2Test(unittest.TestCase):
    def test_native_request_preserves_items_and_appends_one_trigger(self):
        items = [kb_api.u("全文"), {"type": "compaction", "id": "old",
                 "encrypted_content": "unchanged", "new_field": [1, 2]}]
        original = copy.deepcopy(items)
        with mock.patch.object(kb_api, "http", return_value=success()) as request:
            output, _, usage = kb_api.compact(items, model="gpt-test", effort="high")
        path, body = request.call_args.args
        self.assertEqual(path, "/responses")
        self.assertEqual(body["input"], original + [{"type": "compaction_trigger"}])
        self.assertEqual(items, original)
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["instructions"], kb_api.CHARTER)
        self.assertEqual(body["reasoning"], {"effort": "high"})
        self.assertTrue(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(output[0]["future_metadata"], {"keep": True})
        self.assertEqual(usage["output_tokens"], 3000)

    def test_custom_api_instructions_survive_first_stage_redraw(self):
        instructions = " 質問・検索に使う知識を保持。\n\nパスも残す。\n"
        with mock.patch.object(kb_api, "http", side_effect=[
            success(output_tokens=100), success(),
        ]) as request:
            _, _, usage = kb_repo.compact_one(
                "pack", "全文", 2000, "gpt-test", "high", instructions=instructions,
            )
        self.assertEqual(request.call_count, 2)
        self.assertTrue(all(call.args[1]["instructions"] == instructions for call in request.call_args_list))
        self.assertEqual(usage["output_tokens"], 3000)

    def test_all_encrypted_compaction_types_survive_stage_one(self):
        for kind in kb_api.COMPACTION_TYPES:
            with self.subTest(kind=kind):
                item = {"type": kind, "id": "b", "encrypted_content": "opaque", "unknown": 7}
                with mock.patch.object(kb_api, "http", return_value=success(item)):
                    blobs, _, _ = kb_repo.compact_one("pack", "source", 2000, "gpt-test", "low")
                self.assertEqual(blobs, [item])

    def test_second_stage_inherits_or_overrides_api_instructions_through_redraw(self):
        saved = "一次生成で使った指示。\n"
        for override in (None, "別の指示。\n", ""):
            state = {"blobs": [
                {"type": "compaction", "id": f"b-{i}", "encrypted_content": f"opaque-{i}"}
                for i in range(2)
            ], "rounds": [], "instructions": saved}
            with self.subTest(override=override), \
                    mock.patch.object(kb_api, "load_state", return_value=state), \
                    mock.patch.object(kb_api, "save_state"), \
                    mock.patch("shutil.copy"), \
                    mock.patch.object(kb_api, "http", side_effect=[
                        success(output_tokens=100), success(),
                    ]) as request:
                kb_api.cmd_merge_old("fixture", 0, blob_count=2, instructions=override)
            self.assertEqual(request.call_count, 2)
            for call in request.call_args_list:
                self.assertEqual(call.args[1]["instructions"], saved if override is None else override)

    def test_multiline_crlf_comments_and_done_marker(self):
        wire = b': ping\r\nevent: event\r\ndata: {"type":\r\ndata: "future.event"}\r\n\r\ndata: [DONE]\r\n\r\n'
        self.assertEqual(list(kb_api.events(io.BytesIO(wire))), [{"type": "future.event"}])

    def test_requires_blob_and_completion_not_text_fallback(self):
        cases = [
            sse({"type": "response.completed", "response": {"output": []}}),
            sse({"type": "response.output_item.done", "item": kb_api.a("summary")},
                {"type": "response.completed"}),
            sse({"type": "response.output_item.done", "item": {
                "type": "compaction", "encrypted_content": "opaque"}}),
            success({"type": "compaction", "encrypted_content": ""}),
        ]
        for response in cases:
            with self.subTest(wire=response.getvalue()), self.assertRaises(ValueError):
                kb_api.compaction_result(response)

    def test_failed_and_incomplete_streams_do_not_return_earlier_blob(self):
        for kind in ("error", "response.failed", "response.incomplete"):
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, kind):
                kb_api.compaction_result(sse(
                    {"type": "response.output_item.done", "item": {
                        "type": "compaction", "encrypted_content": "opaque"}},
                    {"type": kind}, {"type": "response.completed"},
                ))

    def test_duplicate_compaction_is_not_silently_selected(self):
        item = {"type": "compaction", "encrypted_content": "opaque"}
        with self.assertRaises(ValueError):
            kb_api.compaction_result(sse(
                {"type": "response.output_item.done", "item": item},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed"},
            ))

    def test_existing_retry_for_503_and_bounded_retry_after(self):
        with mock.patch.object(kb_api, "http", side_effect=[
            http_error(503), http_error(429, {"Retry-After": "120"}), success()
        ]) as request, mock.patch.object(kb_api.random, "random", return_value=0), \
                mock.patch.object(kb_api.time, "sleep") as sleep:
            output, _, _ = kb_api.compact([])
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(30)])
        self.assertEqual(output[0]["type"], "compaction")

    def test_bad_request_and_auth_rejection_are_not_retried(self):
        for code in (400, 401):
            with self.subTest(code=code), mock.patch.object(kb_api, "http", side_effect=http_error(code)) as request:
                with self.assertRaises((RuntimeError, urllib.error.HTTPError)):
                    kb_api.compact([])
                self.assertEqual(request.call_count, 1)

    def test_pool_client_auth_and_rr_route_without_codex_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "client.key"
            key.write_text("test-key\n")
            settings = {"KB_POOL_ORIGIN": "http://localhost:1234", "KB_POOL_KEY_FILE": str(key)}
            opener = mock.Mock()
            opener.open.return_value = success()
            with mock.patch.dict(kb_api.os.environ, settings, clear=True), \
                    mock.patch.object(kb_api.urllib.request, "build_opener", return_value=opener):
                with kb_api.http("/responses", {}, stream=True) as response:
                    kb_api.compaction_result(response)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, "http://localhost:1234/_pool/rr/responses")
            self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
            self.assertIsNone(request.get_header("Chatgpt-account-id"))

    def test_missing_pool_configuration_never_reads_codex_auth(self):
        with mock.patch("pathlib.Path.read_text") as read:
            with self.assertRaises(ValueError):
                kb_api.pool_configuration({})
            read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
