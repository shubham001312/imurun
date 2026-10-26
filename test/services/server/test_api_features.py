"""Feature routes: static shell, status, sessions, skills, turns, SSE, OAuth."""
import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from services.connectors.oauth import OAuthFlow, OAuthProvider
from services.connectors.providers.github import GitHubConnector
from services.connectors.registry import ConnectorRegistry
from services.connectors.tokens import MemoryTokenStore
from services.persona.store import PersonaStore
from services.prompts.queue import PromptQueue
from services.server.api import ApiServer
from services.sessions.session import Session
from services.skills.store import SkillsStore


class FakeModel:
    def __init__(self, answer="done"):
        self.answer = answer
        self.calls = []

    def generate(self, messages):
        self.calls.append([dict(message) for message in messages])
        return self.answer


class BlockingModel:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def generate(self, messages):
        self.started.set()
        self.release.wait(timeout=5)
        return "late"


class FailingModel:
    def generate(self, messages):
        raise RuntimeError("engine died")


class NoopTransport:
    def request(self, method, url, headers, json=None):
        return {}


class OAuthTransport:
    def post_form(self, url, data):
        return {"access_token": "tok-1"}


class FeatureTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-api-f-"))
        self.web = self.dir / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text(
            "<!doctype html><meta name='imurun-token' content='__IMURUN_TOKEN__'>",
            encoding="utf-8",
        )
        (self.web / "app.js").write_text("console.log(1);", encoding="utf-8")
        (self.dir / "secret.txt").write_text("nope", encoding="utf-8")
        self.model = FakeModel()
        self.tokens = MemoryTokenStore()
        self.registry = ConnectorRegistry()
        self.registry.register(GitHubConnector(NoopTransport(), self.tokens))
        self.server = self._server(model=self.model).start()

    def tearDown(self):
        self.server.stop()

    def _server(self, *, model, mark=None):
        return ApiServer(
            persona_store=PersonaStore(self.dir / "persona.json"),
            connectors=self.registry,
            queue=PromptQueue(),
            sessions_root=self.dir / "sessions",
            skills=SkillsStore(self.dir / "skills"),
            static_root=self.web,
            model=model,
            mark=mark or (lambda *_args: None),
        )

    def request(self, method, path, body=None, token=True, server=None):
        server = server or self.server
        connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {server.token}"
        payload = json.dumps(body) if body is not None else None
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def raw(self, method, path, token=True, host=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        if host is not None:
            connection.putrequest(method, path, skip_host=True)
            connection.putheader("Host", host)
            connection.endheaders()
        else:
            headers = {}
            if token:
                headers["Authorization"] = f"Bearer {self.server.token}"
            connection.request(method, path, headers=headers)
        response = connection.getresponse()
        status = response.status
        headers = dict(response.getheaders())
        body = response.read()
        connection.close()
        return status, headers, body

    def _session(self):
        status, data = self.request("POST", "/api/sessions", {"first_command": "Fix login"})
        self.assertEqual(status, 200)
        return data["id"]

    def test_static_shell_injects_the_token_and_assets_are_served(self):
        status, headers, body = self.raw("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.server.token.encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, _, body = self.raw("GET", "/app.js", token=False)
        self.assertEqual(status, 200)
        self.assertIn(b"console.log(1)", body)

    def test_a_traversal_outside_the_static_root_is_a_404(self):
        status, _, _ = self.raw("GET", "/%2e%2e/secret.txt", token=False)
        self.assertEqual(status, 404)

    def test_status_reports_the_backend_state(self):
        status, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["model"])
        self.assertEqual(data["runtime"], "ready")
        self.assertEqual(data["sessions"], 0)
        self.assertEqual(data["queue"]["pending"], 0)
        self.assertEqual(data["connectors"]["total"], 1)

    def test_sessions_start_rename_and_read_messages(self):
        session_id = self._session()
        self.assertIn("fix-login", session_id)

        status, data = self.request("GET", "/api/sessions")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in data["sessions"]], [session_id])

        status, data = self.request(
            "POST", f"/api/sessions/{session_id}/rename", {"name": "Renamed"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "Renamed")
        self.assertEqual(data["id"], session_id)

        status, data = self.request("GET", f"/api/sessions/{session_id}/messages")
        self.assertEqual(status, 200)
        self.assertEqual(data["messages"], [])

        status, _ = self.request("GET", "/api/sessions/nope/messages")
        self.assertEqual(status, 404)

    def test_skills_create_list_and_never_expose_paths(self):
        payload = {
            "name": "Deploy check",
            "description": "Check a deploy",
            "body": "1. run the tests",
        }
        status, data = self.request("POST", "/api/skills", payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], "deploy-check")

        status, data = self.request("GET", "/api/skills")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["skills"]), 1)
        self.assertEqual(data["skills"][0]["body"], "1. run the tests")
        self.assertNotIn("path", data["skills"][0])

        status, _ = self.request("POST", "/api/skills", payload)
        self.assertEqual(status, 409)

    def test_a_queued_prompt_publishes_a_queue_event(self):
        session_id = self._session()
        subscription = self.server._events.subscribe()
        status, _ = self.request(
            "POST", "/api/prompts", {"session_id": session_id, "text": "next"}
        )
        self.assertEqual(status, 200)
        event = subscription.get(timeout=1)
        self.assertEqual(event.type, "queue")
        self.assertEqual(event.data["session_id"], session_id)
        self.assertEqual(event.data["pending"], 1)

    def test_a_turn_runs_and_appears_in_the_transcript(self):
        session_id = self._session()
        status, data = self.request(
            "POST", "/api/turns", {"session_id": session_id, "text": "hello"}
        )
        self.assertEqual(status, 200)
        turn_id = data["turn_id"]

        deadline = time.time() + 5
        while time.time() < deadline:
            status, data = self.request("GET", f"/api/turns/{turn_id}")
            self.assertEqual(status, 200)
            if data["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["answer"], "done")
        self.assertEqual(data["steps"], 1)

        status, data = self.request("GET", f"/api/sessions/{session_id}/messages")
        self.assertEqual(status, 200)
        self.assertEqual(
            [message["role"] for message in data["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(data["messages"][1]["content"], "done")

    def test_cancel_stops_a_running_turn(self):
        session_id = self._session()
        blocking = BlockingModel()
        self.server._turns._model = blocking
        status, data = self.request(
            "POST", "/api/turns", {"session_id": session_id, "text": "slow"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(blocking.started.wait(timeout=5))
        turn_id = data["turn_id"]

        status, data = self.request("POST", f"/api/turns/{turn_id}/cancel")
        self.assertEqual(status, 200)
        self.assertTrue(data["cancelled"])
        blocking.release.set()

        deadline = time.time() + 5
        while time.time() < deadline:
            status, data = self.request("GET", f"/api/turns/{turn_id}")
            if data["state"] != "running":
                break
            time.sleep(0.02)
        self.assertEqual(data["state"], "stopped")

    def test_a_failing_turn_is_marked_and_the_server_survives(self):
        session_id = self._session()
        marks = []
        other = self._server(
            model=FailingModel(), mark=lambda *args: marks.append(args)
        ).start()
        try:
            status, data = self.request(
                "POST",
                "/api/turns",
                {"session_id": session_id, "text": "boom"},
                server=other,
            )
            self.assertEqual(status, 200)
            turn_id = data["turn_id"]

            deadline = time.time() + 5
            while time.time() < deadline:
                status, data = self.request(
                    "GET", f"/api/turns/{turn_id}", server=other
                )
                if data["state"] != "running":
                    break
                time.sleep(0.02)
            self.assertEqual(data["state"], "error")
            self.assertIn("RuntimeError", data["error"])
            self.assertTrue(
                any(args[0] == "G3-turn" and args[1] == turn_id for args in marks)
            )

            status, data = self.request(
                "GET", f"/api/sessions/{session_id}/messages", server=other
            )
            self.assertEqual(
                [message["role"] for message in data["messages"]], ["user"]
            )
            status, _ = self.request("GET", "/api/status", server=other)
            self.assertEqual(status, 200)
        finally:
            other.stop()

    def test_a_turn_without_a_model_is_a_503(self):
        other = self._server(model=None).start()
        try:
            session = Session.start("no model", self.dir / "sessions")
            connection = http.client.HTTPConnection("127.0.0.1", other.port, timeout=5)
            payload = json.dumps({"session_id": session.id, "text": "hello"})
            connection.request(
                "POST",
                "/api/turns",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {other.token}",
                },
            )
            response = connection.getresponse()
            self.assertEqual(response.status, 503)
            connection.close()
        finally:
            other.stop()

    def test_events_stream_starts_with_a_retry_frame(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        connection.request(
            "GET",
            "/api/events",
            headers={"Authorization": f"Bearer {self.server.token}"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))
        self.assertIn(b"retry: 3000", response.fp.readline())
        connection.close()

    def test_connect_returns_pkce_and_the_callback_stores_the_token_once(self):
        provider = OAuthProvider(
            id="github",
            authorize_url="https://github.com/login/oauth/authorize",
            token_url="https://github.com/login/oauth/access_token",
            scopes=("repo",),
            client_id="client-1",
        )
        flow = OAuthFlow(
            provider,
            OAuthTransport(),
            self.tokens,
            redirect_uri=f"http://127.0.0.1:{self.server.port}/oauth/callback",
        )
        self.server.add_oauth_flow("github", flow)

        status, data = self.request("POST", "/api/connectors/github/connect", {})
        self.assertEqual(status, 200)
        self.assertIn("code_challenge_method=S256", data["url"])
        self.assertIn("state=", data["url"])

        status, _, body = self.raw(
            "GET",
            f"/oauth/callback?state={data['state']}&code=code-1",
            token=False,
        )
        self.assertEqual(status, 200)
        self.assertIn(b"Connected", body)
        self.assertEqual(self.tokens.get("github"), "tok-1")

        status, _, _ = self.raw(
            "GET",
            f"/oauth/callback?state={data['state']}&code=code-1",
            token=False,
        )
        self.assertEqual(status, 400)

    def test_a_non_loopback_host_is_refused(self):
        status, _, _ = self.raw("GET", "/health", host="evil.example")
        self.assertEqual(status, 403)
