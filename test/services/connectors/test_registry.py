"""Connector registry and token handling."""
import unittest

from services.connectors.registry import ConnectorRegistry
from services.connectors.tokens import MemoryTokenStore, redact


class FakeConnector:
    id = "fake"
    label = "Fake"

    def connected(self) -> bool:
        return True

    def status(self) -> dict:
        return {"id": self.id, "label": self.label, "connected": True, "capabilities": []}


class RegistryTests(unittest.TestCase):
    def test_register_get_list(self):
        registry = ConnectorRegistry()
        connector = FakeConnector()
        registry.register(connector)
        self.assertIs(registry.get("fake"), connector)
        self.assertEqual(registry.ids(), ["fake"])
        self.assertEqual(registry.list()[0]["label"], "Fake")

    def test_duplicate_registration_is_refused(self):
        registry = ConnectorRegistry()
        registry.register(FakeConnector())
        with self.assertRaises(ValueError):
            registry.register(FakeConnector())

    def test_unknown_connector_returns_none(self):
        self.assertIsNone(ConnectorRegistry().get("missing"))


class TokenTests(unittest.TestCase):
    def test_memory_store_roundtrip(self):
        store = MemoryTokenStore()
        store.set("github", "token-1")
        self.assertEqual(store.get("github"), "token-1")
        store.delete("github")
        self.assertIsNone(store.get("github"))

    def test_validation(self):
        store = MemoryTokenStore()
        with self.assertRaises(ValueError):
            store.set("", "token")
        with self.assertRaises(ValueError):
            store.set("github", "")

    def test_redaction_never_reveals_the_token(self):
        self.assertEqual(redact("abcdefgh"), "****efgh")
        self.assertEqual(redact("abc"), "****")
        self.assertEqual(redact(None), "(none)")


if __name__ == "__main__":
    unittest.main()
