"""E2: skills stored in .imurun/skills, created by the agent, used by any session."""
import os
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
