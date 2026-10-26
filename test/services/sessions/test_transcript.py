"""Transcripts are append-only JSONL: ordered, bounded, and corrupt lines marked."""
import tempfile
import unittest
from pathlib import Path

from services.sessions.transcript import TranscriptStore


class TranscriptTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-transcript-"))
        self.marks = []
        self.store = TranscriptStore(
            self.dir, mark=lambda *args: self.marks.append(args)
        )

    def test_append_and_read_in_order(self):
        self.store.append("s1", "user", "hello", "turn-1")
        self.store.append("s1", "assistant", "hi", "turn-1")
        messages = self.store.read("s1")
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual([message.content for message in messages], ["hello", "hi"])
        self.assertEqual(messages[0].turn_id, "turn-1")

    def test_read_returns_the_last_messages_up_to_the_limit(self):
        for index in range(5):
            self.store.append("s1", "user", f"m{index}")
        messages = self.store.read("s1", limit=2)
        self.assertEqual([message.content for message in messages], ["m3", "m4"])

    def test_a_corrupt_line_is_marked_and_skipped(self):
        self.store.append("s1", "user", "one")
        path = self.dir / "s1.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        messages = self.store.read("s1")
        self.assertEqual([message.content for message in messages], ["one"])
        self.assertTrue(any("corrupt" in args[2] for args in self.marks))

    def test_an_unknown_role_is_marked_and_skipped(self):
        self.store.append("s1", "user", "one")
        path = self.dir / "s1.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"role": "root", "content": "nope", "at": 1}\n')
        messages = self.store.read("s1")
        self.assertEqual(len(messages), 1)
        self.assertTrue(any("unknown role" in args[2] for args in self.marks))

    def test_roles_are_validated(self):
        with self.assertRaises(ValueError):
            self.store.append("s1", "root", "nope")
        with self.assertRaises(ValueError):
            self.store.append("s1", "user", "")

    def test_an_unknown_session_reads_as_empty(self):
        self.assertEqual(self.store.read("missing"), [])

    def test_a_session_id_is_required(self):
        with self.assertRaises(ValueError):
            self.store.append("", "user", "hello")


if __name__ == "__main__":
    unittest.main()
