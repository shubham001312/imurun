"""The six remaining providers: same gate, same rules, real request shapes."""
import unittest

from services.connectors.base import KINDS
from services.connectors.gate import ConnectorGate
from services.connectors.providers import (
    CanvaConnector,
    CloudflareConnector,
    RenderConnector,
    SlackConnector,
    SupabaseConnector,
    VercelConnector,
)
from services.connectors.tokens import MemoryTokenStore
from services.safety.decisions import PermissionRequired


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, headers, json=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json})
        return {"ok": True}


CASES = [
    {
        "name": "slack",
        "cls": SlackConnector,
        "read": ("list_channels", {}, "GET", "https://slack.com/api/conversations.list"),
        "write": (
            "post_message",
            {"channel": "C1", "text": "hi"},
            "POST",
            "https://slack.com/api/chat.postMessage",
            {"channel": "C1", "text": "hi"},
        ),
    },
    {
        "name": "vercel",
        "cls": VercelConnector,
        "read": ("list_projects", {}, "GET", "https://api.vercel.com/v9/projects"),
        "write": (
            "create_deployment",
            {"name": "app"},
            "POST",
            "https://api.vercel.com/v13/deployments",
            {"name": "app"},
        ),
    },
    {
        "name": "render",
        "cls": RenderConnector,
        "read": ("list_services", {}, "GET", "https://api.render.com/v1/services"),
        "write": (
            "trigger_deploy",
            {"service": "srv-1"},
            "POST",
            "https://api.render.com/v1/services/srv-1/deploys",
            {},
        ),
    },
    {
        "name": "cloudflare",
        "cls": CloudflareConnector,
        "read": ("list_zones", {}, "GET", "https://api.cloudflare.com/client/v4/zones"),
        "write": (
            "create_dns_record",
            {"zone": "z1", "type": "A", "name": "www", "content": "1.2.3.4"},
            "POST",
            "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
            {"type": "A", "name": "www", "content": "1.2.3.4"},
        ),
    },
    {
        "name": "supabase",
        "cls": SupabaseConnector,
        "read": ("list_projects", {}, "GET", "https://api.supabase.com/v1/projects"),
        "write": (
            "create_project",
            {"name": "db", "organization": "org1", "db_pass": "pw"},
            "POST",
            "https://api.supabase.com/v1/projects",
            {"name": "db", "organization_id": "org1", "db_pass": "pw"},
        ),
    },
    {
        "name": "canva",
        "cls": CanvaConnector,
        "read": ("list_designs", {}, "GET", "https://api.canva.com/rest/v1/designs"),
        "write": (
            "create_design",
            {"design_type": "poster"},
            "POST",
            "https://api.canva.com/rest/v1/designs",
            {"design_type": "poster"},
        ),
    },
]


class ProviderTableTests(unittest.TestCase):
    def setUp(self):
        self.tokens = MemoryTokenStore()
        self.transport = FakeTransport()
        self.marks: list[tuple[str, str, str]] = []
        self.gate = ConnectorGate(mark=lambda rule, subject, why: self.marks.append((rule, subject, why)))

    def test_every_provider_requires_a_token(self):
        for case in CASES:
            with self.subTest(provider=case["name"]):
                connector = case["cls"](self.transport, self.tokens)
                self.assertFalse(connector.connected())
                with self.assertRaises(Exception):
                    self.gate.execute(connector, case["read"][0], case["read"][1])
                self.assertEqual(self.transport.calls, [])

    def test_every_read_runs_and_uses_the_token(self):
        for case in CASES:
            with self.subTest(provider=case["name"]):
                self.tokens.set(case["name"], f"{case['name']}-token")
                connector = case["cls"](self.transport, self.tokens)
                action, params, method, url = case["read"]
                self.assertEqual(self.gate.execute(connector, action, params), {"ok": True})
                call = self.transport.calls[-1]
                self.assertEqual(call["method"], method)
                self.assertEqual(call["url"], url)
                self.assertIn(f"{case['name']}-token", call["headers"]["Authorization"])
                self.assertNotIn(f"{case['name']}-token", " ".join(mark[2] for mark in self.marks))

    def test_every_write_asks_first(self):
        for case in CASES:
            with self.subTest(provider=case["name"]):
                self.tokens.set(case["name"], f"{case['name']}-token")
                connector = case["cls"](self.transport, self.tokens)
                action, params, _method, _url, _body = case["write"]
                self.assertEqual(self.gate.check(connector, action).kind, "ask")
                before = len(self.transport.calls)
                with self.assertRaises(PermissionRequired):
                    self.gate.execute(connector, action, params)
                self.assertEqual(len(self.transport.calls), before)

    def test_every_write_runs_when_approved(self):
        for case in CASES:
            with self.subTest(provider=case["name"]):
                self.tokens.set(case["name"], f"{case['name']}-token")
                connector = case["cls"](self.transport, self.tokens)
                action, params, method, url, body = case["write"]
                self.gate.execute(connector, action, params, approved=True)
                call = self.transport.calls[-1]
                self.assertEqual(call["method"], method)
                self.assertEqual(call["url"], url)
                self.assertEqual(call["json"], body)

    def test_capabilities_use_known_kinds(self):
        for case in CASES:
            with self.subTest(provider=case["name"]):
                connector = case["cls"](self.transport, self.tokens)
                for capability in connector.capabilities:
                    self.assertIn(capability.kind, KINDS)

    def test_provider_ids_are_unique(self):
        ids = [case["cls"].id for case in CASES]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
