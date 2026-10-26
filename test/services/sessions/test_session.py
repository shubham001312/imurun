"""E2: readable session ids, name from the first command, rename, isolation."""
import json
import os
import re
import stat
import tempfile
import unittest

from services.safety.decisions import PermissionRequired
from services.sessions.session import Session, new_session_id, slugify


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="imu-sessions-")

    def test_id_is_readable_and_stable_in_shape(self):
        session = Session.start("Fix the login bug", root=self.root)
        self.assertTrue(session.id.startswith("fix-the-login-bug-"))
        self.assertRegex(session.id, r"^[a-z0-9-]+-[0-9a-f]{4}$")

    def test_name_comes_from_the_first_command(self):
        session = Session.start("Fix the login bug in auth", root=self.root)
        self.assertEqual(session.name, "Fix the login bug in auth")

    def test_name_is_capped_at_sixty_characters(self):
        command = "a" * 100
        session = Session.start(command, root=self.root)
        self.assertEqual(len(session.name), 60)

    def test_rename_changes_the_name_but_not_the_id(self):
        session = Session.start("Fix the login bug", root=self.root)
        original = session.id
        identity = session.rename("Auth cleanup")
        self.assertEqual(identity.name, "Auth cleanup")
        self.assertEqual(identity.id, original)
        reloaded = Session.load(original, root=self.root)
        self.assertEqual(reloaded.name, "Auth cleanup")
        self.assertEqual(reloaded.id, original)

    def test_rename_rejects_empty_and_too_long(self):
        session = Session.start("Fix the login bug", root=self.root)
        with self.assertRaises(ValueError):
            session.rename("   ")
        with self.assertRaises(ValueError):
            session.rename("x" * 81)

    def test_first_command_is_required(self):
        with self.assertRaises(ValueError):
            Session.start("   ", root=self.root)

    def test_slugify_keeps_words(self):
        self.assertEqual(slugify("Fix the login bug!"), "fix-the-login-bug")

    def test_ids_are_unique(self):
        first = new_session_id("same command")
        second = new_session_id("same command")
        self.assertNotEqual(first, second)

    def test_list_sessions_returns_identities(self):
        Session.start("First session", root=self.root)
        Session.start("Second session", root=self.root)
        identities = Session.list_sessions(root=self.root)
        self.assertEqual(len(identities), 2)
        self.assertTrue(all(identity.name for identity in identities))


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="imu-sessions-")

    def test_each_session_has_its_own_memory(self):
        first = Session.start("First session", root=self.root)
        second = Session.start("Second session", root=self.root)
        first.remember("owner", "first")
        self.assertEqual(first.recall("owner"), "first")
        self.assertIsNone(second.recall("owner"))

    def test_memory_survives_a_reload(self):
        session = Session.start("Persistent session", root=self.root)
        session.remember("project", "IMURUN")
        session.remember("notes", {"step": 2})
        reloaded = Session.load(session.id, root=self.root)
        self.assertEqual(reloaded.recall("project"), "IMURUN")
        self.assertEqual(reloaded.recall("notes"), {"step": 2})

    def test_text_is_kept_verbatim(self):
        session = Session.start("Unicode session", root=self.root)
        text = "line one\nline two - ünïcode"
        session.remember("text", text)
        self.assertEqual(Session.load(session.id, root=self.root).recall("text"), text)

    def test_forget_and_clear(self):
        session = Session.start("Forget session", root=self.root)
        session.remember("a", 1)
        session.remember("b", 2)
        self.assertTrue(session.forget("a"))
        self.assertFalse(session.forget("a"))
        session.clear()
        self.assertEqual(session.notes(), {})

    def test_cross_session_read_is_refused_by_default_and_marked(self):
        target = Session.start("Target session", root=self.root)
        target.remember("secret", "value")
        marks: list[tuple[str, str, str]] = []
        with self.assertRaises(PermissionRequired):
            Session.read_other(
                target.id,
                root=self.root,
                requester="other",
                mark=lambda rule, subject, why: marks.append((rule, subject, why)),
            )
        self.assertEqual(marks[0][0], "G3-cross-session")
        self.assertEqual(marks[0][1], target.id)

    def test_cross_session_read_works_with_a_user_grant(self):
        target = Session.start("Target session", root=self.root)
        target.remember("secret", "value")
        memory = Session.read_other(target.id, root=self.root, allowed=True, requester="user")
        self.assertEqual(memory["secret"], "value")

    def test_files_are_private_on_posix(self):
        session = Session.start("Private session", root=self.root)
        if os.name == "nt":
            self.assertTrue(os.path.exists(session._path))
            return
        mode = stat.S_IMODE(os.stat(session._path).st_mode)
        self.assertEqual(mode, 0o600)


class SessionSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="imu-sessions-")

    def test_a_corrupt_session_file_is_marked(self):
        with open(os.path.join(self.root, "broken.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        marks = []
        sessions = Session.list_sessions(
            self.root,
            mark=lambda rule, subject, why: marks.append((rule, subject, why)),
        )
        self.assertEqual(sessions, [])
        self.assertEqual(marks[0][0], "G3-session")

    def test_load_rejects_a_file_with_another_id(self):
        session = Session.start("hello world", root=self.root)
        path = os.path.join(self.root, f"{session.id}.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        data["id"] = "someone-else"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        with self.assertRaises(ValueError):
            Session.load(session.id, root=self.root)

    def test_a_newline_in_the_first_command_does_not_enter_the_name(self):
        name = Session.start("hello\nworld", root=self.root).name
        self.assertNotIn("\n", name)


if __name__ == "__main__":
    unittest.main()
