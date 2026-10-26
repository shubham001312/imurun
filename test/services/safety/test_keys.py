"""Batch G: placeholder keys refused, keys never echoed."""
import unittest

from services.safety.keys import InvalidKey, redact, scrub, validate_key


class KeyTests(unittest.TestCase):
    def test_a_real_key_passes_and_is_trimmed(self):
        self.assertEqual(validate_key("  ghp_abcdef123456  "), "ghp_abcdef123456")

    def test_short_keys_are_refused(self):
        with self.assertRaises(InvalidKey):
            validate_key("abc")

    def test_placeholders_are_refused(self):
        for key in ("changeme", "your-api-key", "replace-me", "todo", "null", "secret"):
            with self.subTest(key=key):
                with self.assertRaises(InvalidKey):
                    validate_key(key)

    def test_a_repeated_character_is_refused(self):
        with self.assertRaises(InvalidKey):
            validate_key("aaaaaaaa")

    def test_a_non_string_is_refused(self):
        with self.assertRaises(InvalidKey):
            validate_key(None)

    def test_the_error_never_contains_the_key(self):
        secret = "your-super-secret-key"
        try:
            validate_key(secret)
        except InvalidKey as exc:
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("the placeholder key was accepted")

    def test_redaction_hides_the_body(self):
        self.assertEqual(redact("ghp_abcdef123456"), "****3456")
        self.assertEqual(redact("short"), "****")
        self.assertEqual(redact(None), "(none)")

    def test_scrub_removes_an_accidental_key(self):
        key = "ghp_abcdef123456"
        text = f"request failed with {key} in the header"
        cleaned = scrub(text, key)
        self.assertNotIn(key, cleaned)
        self.assertIn("****3456", cleaned)

    def test_scrub_without_a_key_is_a_no_op(self):
        self.assertEqual(scrub("hello", None), "hello")


if __name__ == "__main__":
    unittest.main()
