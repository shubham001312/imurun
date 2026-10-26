"""Persona tool: structured, sanitised, failure-safe reads for the model."""
import tempfile
import unittest
from pathlib import Path

from services.persona.store import Persona, PersonaStore
from services.persona.tool import PersonaTool


class PersonaToolTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="imu-persona-")) / "persona.json"
        self.store = PersonaStore(self.path)
        self.tool = PersonaTool(self.store)

    def test_unconfigured_returns_a_clean_view(self):
        view = self.tool.read()
        self.assertFalse(view.configured)
        self.assertEqual(view.error, "")

    def test_configured_returns_the_fields(self):
        self.store.save(Persona("Imu", "Founder", "Short and direct."))
        view = self.tool.read()
        self.assertTrue(view.configured)
        self.assertEqual(view.name, "Imu")
        self.assertEqual(view.email_role, "Founder")
        self.assertEqual(view.description, "Short and direct.")

    def test_corrupted_file_returns_an_error_not_an_exception(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        view = self.tool.read()
        self.assertFalse(view.configured)
        self.assertTrue(view.error)
        self.assertNotIn(str(self.path), view.error)

    def test_the_view_never_carries_the_file_path(self):
        self.store.save(Persona("Imu", "Founder"))
        view = self.tool.read()
        self.assertNotIn(str(self.path), repr(view))


    def test_an_oversized_persona_file_returns_an_error(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
        view = self.tool.read()
        self.assertFalse(view.configured)
        self.assertTrue(view.error)


if __name__ == "__main__":
    unittest.main()
