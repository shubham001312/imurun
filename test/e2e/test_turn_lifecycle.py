"""L5 end to end: one turn through the real API, SSE, stores, and transcript."""
import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path

from services.persona.store import PersonaStore
from services.prompts.queue import PromptQueue
from services.server.api import ApiServer
from services.skills.store import SkillsStore


class FakeModel:
    def generate(self, messages):
        return "hello back"


class TurnLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-e2e-"))
        self.web = self.dir / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text(
            "<meta name='imurun-token' content='__IMURUN_TOKEN__'>", encoding="utf-8"
        )
        self.server = ApiServer(
            persona_store=PersonaStore(self.dir / "persona.json"),
            queue=PromptQueue(),
            sessions_root=self.dir / "sessions",
            skills=SkillsStore(self.dir / "skills"),
            static_root=self.web,
            model=FakeModel(),
            mark=lambda *_args: None,
        ).start()

    def tearDown(self):
        self.server.stop()

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=5
        )
        connection.request(
            method,
            path,
            body=json.dumps(body) if body is not None else None,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.server.token}",
            },
        )
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def read_event(self, response):
        event = None
        data = None
        while True:
            line = response.fp.readline()
            if not line:
                return None
            text = line.decode("utf-8").rstrip("\r\n")
            if not text:
                if event is not None:
                    return event, json.loads(data) if data else {}
                continue
            if text.startswith(":"):
                continue
            if text.startswith("event: "):
                event = text[7:]
            elif text.startswith("data: "):
                data = text[6:]

    def test_a_turn_streams_ends_and_persists(self):
        status, data = self.request(
            "POST", "/api/sessions", {"first_command": "first command"}
        )
        self.assertEqual(status, 200)
        session_id = data["id"]

        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.port, timeout=5
        )
        connection.request(
            "GET",
            "/api/events",
            headers={"Authorization": f"Bearer {self.server.token}"},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"retry: 3000", response.fp.readline())

        status, data = self.request(
            "POST", "/api/turns", {"session_id": session_id, "text": "hello"}
        )
        self.assertEqual(status, 200)
        turn_id = data["turn_id"]

        events = []
        deadline = time.time() + 5
        while time.time() < deadline:
            event = self.read_event(response)
            if event is None:
                break
            events.append(event)
            if event[0] == "turn.end":
                break
        connection.close()

        names = [name for name, _ in events]
        self.assertEqual(names[0], "turn.start")
        self.assertIn("queue", names)
        self.assertIn("step.start", names)
        self.assertIn("step", names)
        self.assertEqual(names[-1], "turn.end")
        self.assertEqual(events[-1][1]["turn_id"], turn_id)
        self.assertEqual(events[-1][1]["answer"], "hello back")

        status, data = self.request(
            "GET", f"/api/sessions/{session_id}/messages"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            [message["role"] for message in data["messages"]],
            ["user", "assistant"],
        )
        self.assertEqual(data["messages"][1]["content"], "hello back")

        status, data = self.request(
            "POST", f"/api/sessions/{session_id}/rename", {"name": "Renamed"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["name"], "Renamed")
        self.assertEqual(data["id"], session_id)

        status, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(data["sessions"], 1)
        self.assertEqual(data["queue"]["pending"], 0)


if __name__ == "__main__":
    unittest.main()
