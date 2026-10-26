"""G3 connector gate: reads free, writes gated, every action audited."""
import unittest

from services.connectors.base import ADMIN, READ, WRITE, Capability, Connector
from services.connectors.gate import ConnectorGate
from services.safety.decisions import PermissionRequired


class FakeConnector(Connector):
    id = "fake"
    label = "Fake"
    capabilities = (
        Capability("read_thing", READ),
        Capability("write_thing", WRITE),
        Capability("admin_thing", ADMIN),
    )

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail
        self.token = "secret-token-1234"

    def connected(self) -> bool:
        return True

    def execute(self, action: str, params: dict) -> dict:
        self.calls.append((action, params))
        if self.fail:
            raise RuntimeError(f"boom with {self.token}")
        return {"ok": True, "action": action}


class GateTests(unittest.TestCase):
    def setUp(self):
        self.marks: list[tuple[str, str, str]] = []
        self.gate = ConnectorGate(mark=lambda rule, subject, why: self.marks.append((rule, subject, why)))

    def test_read_is_allowed_without_approval(self):
        connector = FakeConnector()
        self.assertEqual(self.gate.check(connector, "read_thing").kind, "allow")
        self.assertEqual(self.gate.execute(connector, "read_thing", {})["ok"], True)

    def test_write_asks_and_execute_refuses_without_approval(self):
        connector = FakeConnector()
        self.assertEqual(self.gate.check(connector, "write_thing").kind, "ask")
        with self.assertRaises(PermissionRequired):
            self.gate.execute(connector, "write_thing", {})
        self.assertEqual(connector.calls, [])

    def test_write_runs_with_approval_and_is_marked(self):
        connector = FakeConnector()
        result = self.gate.execute(connector, "write_thing", {"x": 1}, approved=True)
        self.assertEqual(result["action"], "write_thing")
        self.assertEqual(len(connector.calls), 1)
        self.assertEqual(self.marks[-1][0], "G3-connector")
        self.assertEqual(self.marks[-1][1], "fake:write_thing")
        self.assertEqual(self.marks[-1][2], "ok")

    def test_admin_also_asks(self):
        self.assertEqual(self.gate.check(FakeConnector(), "admin_thing").kind, "ask")

    def test_unknown_action_is_denied(self):
        connector = FakeConnector()
        self.assertEqual(self.gate.check(connector, "teleport").kind, "deny")
        with self.assertRaises(PermissionRequired):
            self.gate.execute(connector, "teleport", {})

    def test_failure_is_marked_without_the_secret(self):
        connector = FakeConnector(fail=True)
        with self.assertRaises(Exception):
            self.gate.execute(connector, "read_thing", {})
        joined = " ".join(mark[2] for mark in self.marks)
        self.assertIn("failed", joined)
        self.assertNotIn("secret-token-1234", joined)

    def test_a_broken_mark_does_not_break_the_action(self):
        def broken(_rule, _subject, _why):
            raise RuntimeError("sink down")

        gate = ConnectorGate(mark=broken)
        self.assertEqual(gate.execute(FakeConnector(), "read_thing", {})["ok"], True)


if __name__ == "__main__":
    unittest.main()
