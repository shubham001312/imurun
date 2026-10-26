"""Batch F: durable evidence, bounded rotation, sink failures never lost."""
import json
import tempfile
import threading
import unittest
from pathlib import Path

from services.safety.audit import Audit
from services.safety.sink import JsonlSink, default_sink


class SinkTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-audit-"))
        self.path = self.dir / "logs" / "audit.jsonl"

    def test_events_are_written_as_json_lines(self):
        sink = JsonlSink(self.path)
        audit = Audit(sink=sink)
        audit.mark("G1", "model:start", "ok")
        audit.mark("G3", "github:create_issue", "refused")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["rule"], "G1")
        self.assertEqual(first["subject"], "model:start")
        self.assertEqual(first["why"], "ok")
        self.assertIn("at", first)
        self.assertEqual(json.loads(lines[1])["subject"], "github:create_issue")

    def test_parent_folder_is_created(self):
        self.assertFalse(self.path.parent.exists())
        JsonlSink(self.path)
        self.assertTrue(self.path.parent.exists())

    def test_rotation_caps_files_and_keeps_the_newest(self):
        sink = JsonlSink(self.path, max_bytes=300, backups=2)
        audit = Audit(sink=sink)
        for index in range(20):
            audit.mark("G1", f"event-{index}", "x" * 20)
        files = sorted(self.path.parent.glob("audit.jsonl*"))
        self.assertLessEqual(len(files), 3)
        newest = self.path.read_text(encoding="utf-8").splitlines()
        self.assertIn("event-19", newest[-1])

    def test_rotation_argument_validation(self):
        with self.assertRaises(ValueError):
            JsonlSink(self.path, max_bytes=0)
        with self.assertRaises(ValueError):
            JsonlSink(self.path, backups=0)

    def test_unicode_is_preserved(self):
        audit = Audit(sink=JsonlSink(self.path))
        audit.mark("G1", "sessión", "gründe")
        line = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["subject"], "sessión")
        self.assertEqual(line["why"], "gründe")

    def test_default_sink_targets_the_imurun_logs_folder(self):
        sink = default_sink()
        text = str(sink.path).replace("\\", "/")
        self.assertTrue(text.endswith(".imurun/logs/audit.jsonl"))

    def test_concurrent_marks_write_valid_lines(self):
        audit = Audit(sink=JsonlSink(self.path))

        def worker(index: int) -> None:
            audit.mark("G1", f"thread-{index}", "ok")

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 50)
        subjects = {json.loads(line)["subject"] for line in lines}
        self.assertEqual(len(subjects), 50)


class SinkFailureTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-audit-"))

    def test_a_broken_sink_is_counted_and_never_raises(self):
        def broken(_event):
            raise RuntimeError("disk full")

        audit = Audit(sink=broken, capacity=2)
        for index in range(10):
            event = audit.mark("G1", f"subject-{index}", "why")
            self.assertEqual(event.rule, "G1")
        self.assertEqual(audit.sink_errors, 10)

    def test_the_error_count_survives_ring_trimming(self):
        def broken(_event):
            raise RuntimeError("disk full")

        audit = Audit(sink=broken, capacity=2)
        for index in range(10):
            audit.mark("G1", f"subject-{index}", "why")
        tail = audit.tail(50)
        self.assertEqual(len(tail), 2)
        self.assertEqual(tail[0].rule, "G1")
        self.assertEqual(tail[-1].rule, "G7-sink")
        self.assertEqual(audit.sink_errors, 10)

    def test_audit_without_a_sink_still_works(self):
        audit = Audit()
        audit.mark("G1", "subject", "why")
        self.assertEqual(audit.sink_errors, 0)
        self.assertEqual(audit.tail(1)[0].subject, "subject")


if __name__ == "__main__":
    unittest.main()
