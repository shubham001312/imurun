"""E2: skills stored in .imurun/skills, created by the agent, used by any session."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from services.skills.store import SkillsStore


class SkillsStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="imu-skills-"))
        self.store = SkillsStore(root=self.root)

    def test_create_writes_a_skill_file(self):
        skill = self.store.create("Release Notes", "Prepare release notes", "Read the changelog first.")
        self.assertEqual(skill.id, "release-notes")
        target = self.root / "release-notes" / "SKILL.md"
        self.assertTrue(target.is_file())
        text = target.read_text(encoding="utf-8")
        self.assertIn("name: Release Notes", text)
        self.assertIn("Read the changelog first.", text)

    def test_created_skill_is_listed_and_usable(self):
        self.store.create("Release Notes", "Prepare release notes", "Step one.")
        skills = self.store.list()
        self.assertEqual([skill.id for skill in skills], ["release-notes"])
        loaded = self.store.get("release-notes")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.body, "Step one.")

    def test_agent_created_skill_marks_its_origin(self):
        self.store.create("Debug Helper", "Find root causes", "Trace backwards.", origin="agent")
        skill = self.store.get("debug-helper")
        self.assertEqual(skill.origin, "agent")

    def test_duplicate_creation_is_refused(self):
        self.store.create("Release Notes", "Prepare release notes", "Step one.")
        with self.assertRaises(FileExistsError):
            self.store.create("Release Notes", "Other", "Step two.")

    def test_invalid_skill_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.store.create("Bad", "Bad id", "body", skill_id="Bad_ID")

    def test_empty_fields_are_refused(self):
        with self.assertRaises(ValueError):
            self.store.create("", "description", "body")
        with self.assertRaises(ValueError):
            self.store.create("Name", "", "body")
        with self.assertRaises(ValueError):
            self.store.create("Name", "description", "   ")

    def test_flat_markdown_skills_are_listed(self):
        (self.root / "flat-skill.md").write_text(
            "---\nname: Flat Skill\ndescription: flat file\n---\n\nBody here.\n",
            encoding="utf-8",
        )
        skill = self.store.get("flat-skill")
        self.assertIsNotNone(skill)
        self.assertEqual(skill.description, "flat file")

    def test_body_is_kept_verbatim(self):
        body = "line one\nline two - ünïcode"
        self.store.create("Unicode Skill", "Keeps text", body)
        self.assertEqual(self.store.get("unicode-skill").body, body)

    def test_files_are_private_on_posix(self):
        skill = self.store.create("Private Skill", "Private", "Body")
        if os.name == "nt":
            self.assertTrue(os.path.exists(skill.path))
            return
        mode = os.stat(skill.path).st_mode & 0o777
        self.assertEqual(mode, 0o600)


class SkillSafetyTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="imu-skills-"))
        self.marks: list[tuple[str, str, str]] = []
        self.store = SkillsStore(
            self.root,
            mark=lambda rule, subject, why: self.marks.append((rule, subject, why)),
        )

    def test_a_newline_in_a_field_cannot_inject_frontmatter(self):
        skill = self.store.create("Evil\norigin: root", "desc\nname: hijack", "body")
        parsed = self.store.get(skill.id)
        self.assertIsNotNone(parsed)
        self.assertNotIn("\n", parsed.name)
        self.assertEqual(parsed.origin, "user")

    def test_origin_must_be_a_single_token(self):
        with self.assertRaises(ValueError):
            self.store.create("Name", "desc", "body", origin="user\norigin: root")

    def test_an_unreadable_skill_is_marked(self):
        broken = self.root / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("---\nname: x", encoding="utf-8")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.marks[0][0], "G3-skills")
        self.assertIn("broken", self.marks[0][1])

    def test_a_valid_skill_still_lists_next_to_a_broken_one(self):
        self.store.create("Good", "works", "body")
        broken = self.root / "broken" / "SKILL.md"
        broken.parent.mkdir(parents=True)
        broken.write_text("---\nname: x", encoding="utf-8")
        ids = [skill.id for skill in self.store.list()]
        self.assertIn("good", ids)
        self.assertEqual(len(ids), 1)


    def test_a_flat_skill_file_blocks_the_same_id(self):
        (self.root / "good.md").write_text("# Good", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            self.store.create("Good", "works", "body", skill_id="good")


    def test_a_failed_chmod_is_not_ignored_on_posix(self):
        with mock.patch("services.skills.store.os.chmod", side_effect=OSError("no chmod")):
            if os.name == "nt":
                self.assertIsNotNone(self.store.create("Good", "works", "body"))
            else:
                with self.assertRaises(OSError):
                    self.store.create("Good", "works", "body")


if __name__ == "__main__":
    unittest.main()
