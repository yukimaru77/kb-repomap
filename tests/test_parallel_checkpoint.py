import threading
import time
from pathlib import Path, PurePosixPath
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kb_repo


def item(pack_id, label):
    return (
        pack_id, label, [PurePosixPath(f"{label}.txt")],
        f"source for {label}", 100, False,
    )


def result(blob_id):
    return (
        [{"type": "compaction", "id": blob_id, "encrypted_content": blob_id}],
        1.0,
        {"output_tokens": 100},
    )


class ParallelCheckpointTest(unittest.TestCase):
    def test_completed_results_are_checkpointed_when_earlier_pack_fails(self):
        state = kb_repo.new_state(Path("/tmp/repository"))
        released = threading.Event()

        def compact(label, _source, _threshold, _model, _effort, **_kwargs):
            if label == "first":
                released.wait(timeout=1)
                time.sleep(0.05)
                raise RuntimeError("temporary failure")
            if label == "second":
                released.set()
            return result(f"blob-{label}")

        work = [
            item("pack-first", "first"),
            item("pack-second", "second"),
            item("pack-third", "third"),
        ]
        order = {entry[0]: index for index, entry in enumerate(work)}
        with mock.patch.object(kb_repo, "compact_one", side_effect=compact), \
                mock.patch.object(kb_repo, "save_repo_state") as save:
            with self.assertRaisesRegex(RuntimeError, "1 pack.*first"):
                kb_repo.compact_pending(
                    "test", state, work, order, 3, 2_000, "model", "high",
                )

        self.assertEqual(
            state["fed"], ["pack-second", "pack-third"],
        )
        self.assertEqual(
            [blob["id"] for blob in state["blobs"]],
            ["blob-second", "blob-third"],
        )
        self.assertEqual(save.call_count, 2)

    def test_completion_order_does_not_change_logical_blob_order(self):
        state = kb_repo.new_state(Path("/tmp/repository"))
        second_done = threading.Event()

        def compact(label, _source, _threshold, _model, _effort, **_kwargs):
            if label == "first":
                second_done.wait(timeout=1)
                time.sleep(0.05)
            else:
                second_done.set()
            return result(f"blob-{label}")

        work = [
            item("pack-first", "first"),
            item("pack-second", "second"),
        ]
        order = {entry[0]: index for index, entry in enumerate(work)}
        with mock.patch.object(kb_repo, "compact_one", side_effect=compact), \
                mock.patch.object(kb_repo, "save_repo_state"):
            kb_repo.compact_pending(
                "test", state, work, order, 2, 2_000, "model", "high",
            )

        self.assertEqual(state["fed"], ["pack-second", "pack-first"])
        self.assertEqual(
            [blob["id"] for blob in state["blobs"]],
            ["blob-first", "blob-second"],
        )


if __name__ == "__main__":
    unittest.main()
