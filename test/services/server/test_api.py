"""Loopback API: token gate, body cap, persona, connectors, prompts, sessions."""
import http.client
import json
import tempfile
import unittest
from pathlib import Path

from services.connectors.base import ConnectorError
from services.connectors.providers.github import GitHubConnector
from services.connectors.registry import ConnectorRegistry
from services.connectors.tokens import MemoryTokenStore
from services.persona.store import PersonaStore
from services.prompts.queue import PromptQueue
from services.server.api import ApiServer
from services.sessions.session import Session


class FakeTransport:
    def __init__(self):
        self.calls = []

    def request(self, method, url, headers, json=None):
        self.calls.append({"method": method, "url": url})
        return {"ok": True}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-api-"))
        self.store = PersonaStore(self.dir / "persona.json")
        self.registry = ConnectorRegistry()
        self.queue = PromptQueue()
        self.marks = []
        self.server = ApiServer(
            persona_store=self.store,
            connectors=self.registry,
            queue=self.queue,
            sessions_root=self.dir / "sessions",
            mark=lambda rule, subject, why: self.marks.append((rule, subject, why)),
            max_body=200,
        ).start()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {self.server.token}"
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_a_request_without_the_token_is_refused(self):
        status, data = self.request("GET", "/health", token=False)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "unauthorized")
        self.assertTrue(any(mark[0] == "G3-api" for mark in self.marks))

    def test_health_with_the_token(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"ok": True}))

    def test_persona_roundtrip(self):
        status, _ = self.request(
            "PUT",
            "/api/persona",
            {"name": "Imu", "email_role": "Founder", "description": "Short."},
        )
        self.assertEqual(status, 200)
        status, data = self.request("GET", "/api/persona")
        self.assertEqual(status, 200)
        self.assertTrue(data["configured"])
        self.assertEqual(data["name"], "Imu")
        self.assertEqual(data["email_role"], "Founder")
        self.assertEqual(data["description"], "Short.")
        self.assertNotIn("path", data)

    def test_persona_validation_is_a_400(self):
        status, data = self.request("PUT", "/api/persona", {"name": "", "email_role": "x"})
        self.assertEqual(status, 400)
        self.assertIn("name", data["error"])

    def test_connectors_start_empty(self):
        self.assertEqual(self.request("GET", "/api/connectors"), (200, {"connectors": []}))

    def test_an_unknown_connector_is_a_404(self):
        status, data = self.request("POST", "/api/connectors/nope/actions/read", {})
        self.assertEqual(status, 404)

    def test_a_prompt_is_queued(self):
        status, data = self.request("POST", "/api/prompts", {"session_id": "s1", "text": "hello"})
        self.assertEqual(status, 200)
        self.assertIn("id", data)
        self.assertEqual(len(self.queue.pending("s1")), 1)

    def test_an_oversized_body_is_refused(self):
        status, data = self.request("POST", "/api/prompts", {"session_id": "s1", "text": "x" * 300})
        self.assertEqual(status, 413)
        self.assertIn("too large", data["error"])

    def test_a_write_is_gated_and_runs_only_when_approved(self):
        tokens = MemoryTokenStore()
        tokens.set("github", "gh-token-1234")
        transport = FakeTransport()
        self.registry.register(GitHubConnector(transport, tokens))
        body = {"params": {"repo": "a/b", "title": "Bug"}}

        status, data = self.request("POST", "/api/connectors/github/actions/create_issue", body)
        self.assertEqual(status, 403)
        self.assertEqual(data["decision"], "ask")
        self.assertEqual(transport.calls, [])

        approved = {"params": {"repo": "a/b", "title": "Bug"}, "approved": True}
        status, data = self.request("POST", "/api/connectors/github/actions/create_issue", approved)
        self.assertEqual(status, 200)
        self.assertEqual(data["result"], {"ok": True})
        self.assertEqual(len(transport.calls), 1)

    def test_a_read_runs_without_approval(self):
        tokens = MemoryTokenStore()
        tokens.set("github", "gh-token-1234")
        transport = FakeTransport()
        self.registry.register(GitHubConnector(transport, tokens))
        status, _ = self.request(
            "POST", "/api/connectors/github/actions/list_repos", {"params": {}}
        )
        self.assertEqual(status, 200)

    def test_sessions_are_listed(self):
        Session.start("First session", root=self.dir / "sessions")
        status, data = self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["sessions"]), 1)
        self.assertEqual(data["sessions"][0]["name"], "First session")

    def test_the_server_refuses_a_non_loopback_host(self):
        with self.assertRaises(ValueError):
            ApiServer(persona_store=self.store, host="0.0.0.0")

    def test_the_token_is_long(self):
        self.assertGreaterEqual(len(self.server.token), 20)


if __name__ == "__main__":
    unittest.main()
