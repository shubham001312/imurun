"""L4 multi-client: per-session FIFO order and parallel callers."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from services.persona.store import PersonaStore
from services.prompts.queue import PromptQueue
from services.server.api import ApiServer
from services.skills.store import SkillsStore


class MultiClientQueueTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="imu-api-q-"))
        self.queue = PromptQueue()
        self.server = ApiServer(
            persona_store=PersonaStore(self.dir / "persona.json"),
            queue=self.queue,
            sessions_root=self.dir / "sessions",
            skills=SkillsStore(self.dir / "skills"),
            model=None,
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

    def session(self, first_command):
        status, data = self.request(
            "POST", "/api/sessions", {"first_command": first_command}
        )
        self.assertEqual(status, 200)
        return data["id"]

    def push(self, session_id, text):
        return self.request(
            "POST", "/api/prompts", {"session_id": session_id, "text": text}
        )

    def test_two_clients_keep_their_own_fifo_order(self):
        alpha = self.session("alpha")
        beta = self.session("beta")
        for session_id, text in (
            (alpha, "a1"),
            (beta, "b1"),
            (alpha, "a2"),
            (beta, "b2"),
        ):
            status, _ = self.push(session_id, text)
            self.assertEqual(status, 200)

        self.assertEqual(
            [prompt.text for prompt in self.queue.drain(alpha)], ["a1", "a2"]
        )
        self.assertEqual(
            [prompt.text for prompt in self.queue.drain(beta)], ["b1", "b2"]
        )
        self.assertEqual(self.queue.pending_count(), 0)

    def test_ten_parallel_pushes_all_land(self):
        session_id = self.session("parallel")
        errors = []
        barrier = threading.Barrier(10)

        def worker(index):
            try:
                barrier.wait(timeout=5)
                status, data = self.push(session_id, f"p{index}")
                if status != 200:
                    errors.append((status, data))
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(self.queue.pending_count(), 10)
        self.assertEqual(len(self.queue.drain(session_id)), 10)


if __name__ == "__main__":
    unittest.main()
