from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_api


def blob(name):
    return {"id": name, "type": "compaction", "encrypted_content": "opaque-" + name}


class GroupingTest(unittest.TestCase):
    def test_count_groups_preserve_order_tail_and_optional_measurements(self):
        blobs = [blob(str(i)) for i in range(5)]
        measurements = {"0": 100000, "1": 80000, "2": 50000, "3": 70000, "4": 4000}
        counted = kb_api.plan_count_groups(blobs, measurements, 2)
        self.assertEqual([group[2] for group in counted], [blobs[:2], blobs[2:4], blobs[4:]])
        self.assertEqual([group[3] for group in counted], [180000, 120000, 4000])
        unmeasured = kb_api.plan_count_groups(blobs, {}, 2)
        self.assertEqual([group[2] for group in unmeasured], [group[2] for group in counted])
        self.assertTrue(all(group[3] is None for group in unmeasured))
        token_groups = kb_api.plan_token_groups(blobs, measurements, 150000)
        self.assertEqual([group[2] for group in token_groups], [blobs[:1], blobs[1:3], blobs[3:]])

    def test_count_singleton_and_empty(self):
        self.assertEqual(kb_api.plan_count_groups([], {}, 2), [])
        items = [blob("a"), blob("b")]
        self.assertEqual([group[2] for group in kb_api.plan_count_groups(items, {}, 1)], [[items[0]], [items[1]]])

    def test_count_merge_excludes_prelude_done_and_keeps_unpaired_blob(self):
        prelude, done, *raw = [blob(name) for name in ["prelude", "done", "a", "b", "c", "d", "e"]]
        state = {"blobs": [prelude, done, *raw], "base_items": [], "rounds": [],
                 "prelude_blob_ids": ["prelude"], "stage2_ids": ["done"]}

        def compact(items, **kwargs):
            return [blob("merged-" + items[1]["id"])], 0.1, {"output_tokens": 3000}

        with mock.patch.object(kb_api, "load_state", return_value=state), \
                mock.patch.object(kb_api, "save_state"), \
                mock.patch("shutil.copy"), \
                mock.patch.object(kb_api, "compact", side_effect=compact) as request:
            kb_api.cmd_merge_old("example", 0, blob_count=2)
        self.assertEqual(request.call_count, 2)
        self.assertEqual([call.args[0][1:] for call in request.call_args_list], [raw[:2], raw[2:4]])
        self.assertEqual([x["id"] for x in state["blobs"]], ["prelude", "done", "merged-a", "merged-c", "e"])
        self.assertTrue(all(x["input_blob_tokens"] is None for x in state["rounds"]))


if __name__ == "__main__":
    unittest.main()
