"""G1 persona: private config, sanitised directive, signature by code."""
import os
import tempfile
import unittest
from pathlib import Path

from services.persona.store import Persona, PersonaComposer, PersonaStore


class PersonaStoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp(prefix="imu-persona-")) / "persona.json"
        self.store = PersonaStore(self.path)

    def test_save_and_load_roundtrip(self):
        saved = self.store.save(Persona("Imu", "Founder", "Short and direct."))
        loaded = self.store.load()
        self.assertEqual(saved, loaded)
        self.assertEqual(loaded.name, "Imu")
        self.assertEqual(loaded.email_role, "Founder")
        self.assertEqual(loaded.description, "Short and direct.")

    def test_name_and_role_are_required(self):
        with self.assertRaises(ValueError):
            self.store.save(Persona("", "Founder"))
        with self.assertRaises(ValueError):
            self.store.save(Persona("Imu", "  "))

    def test_control_characters_are_stripped(self):
        saved = self.store.save(Persona("Im\u0000u", "Found\x07er", "ok"))
        self.assertEqual(saved.name, "Imu")
        self.assertEqual(saved.email_role, "Founder")

    def test_values_are_capped(self):
        saved = self.store.save(Persona("n" * 200, "r" * 300, "d" * 900))
        self.assertEqual(len(saved.name), 80)
        self.assertEqual(len(saved.email_role), 120)
        self.assertEqual(len(saved.description), 500)

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.store.load())
        self.assertFalse(self.store.exists())

    def test_file_is_private_on_posix(self):
        self.store.save(Persona("Imu", "Founder"))
        if os.name == "nt":
            self.assertTrue(self.path.exists())
            return
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_unicode_is_preserved(self):
        self.store.save(Persona("Imü", "Gründer", "Kurz und klar."))
        loaded = self.store.load()
        self.assertEqual(loaded.name, "Imü")
        self.assertEqual(loaded.description, "Kurz und klar.")


class PersonaComposerTests(unittest.TestCase):
    def setUp(self):
        self.store = PersonaStore(Path(tempfile.mkdtemp(prefix="imu-persona-")) / "persona.json")
        self.composer = PersonaComposer(self.store)

    def test_empty_directive_without_a_persona(self):
        self.assertEqual(self.composer.directive(), "")
        self.assertEqual(self.composer.apply_signature("hello"), "hello")

    def test_directive_carries_name_role_and_behaviour(self):
        self.store.save(Persona("Imu", "Founder", "Short and direct."))
        directive = self.composer.directive()
        self.assertIn("Imu", directive)
        self.assertIn("Founder", directive)
        self.assertIn("Short and direct.", directive)
        self.assertIn("honestly", directive)

    def test_directive_never_contains_the_file_path(self):
        self.store.save(Persona("Imu", "Founder"))
        self.assertNotIn(str(self.store._path), self.composer.directive())

    def test_signature_is_applied_by_code(self):
        self.store.save(Persona("Imu", "Founder"))
        body = self.composer.apply_signature("Thanks for the update.")
        self.assertTrue(body.startswith("Thanks for the update."))
        self.assertIn("Imu", body)
        self.assertIn("Founder", body)

    def test_signature_is_not_duplicated(self):
        self.store.save(Persona("Imu", "Founder"))
        once = self.composer.apply_signature("Body")
        twice = self.composer.apply_signature(once)
        self.assertEqual(once, twice)

    def test_regex_characters_in_the_name_are_literal(self):
        self.store.save(Persona("A{{name}}B", "Role"))
        body = self.composer.apply_signature("Body")
        self.assertIn("A{{name}}B", body)


if __name__ == "__main__":
    unittest.main()
