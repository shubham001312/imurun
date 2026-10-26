"""A turn publishes the event sequence the chat screen consumes."""
import tempfile
import time
import unittest
from pathlib import Path

from services.prompts.queue import PromptQueue
from services.server.events import EventBroker
from services.server.turns import TurnRunner
from services.sessions.transcript import TranscriptStore


class Model:
    def generate(self, messages):
        return "ok"


class TurnEventTests(unittest.TestCase):
    def test_a_turn_publishes_start_queue_steps_and_end(self):
        events = EventBroker()
        subscription = events.subscribe()
        runner = TurnRunner(
            Model(),
            queue=PromptQueue(),
            transcripts=TranscriptStore(Path(tempfile.mkdtemp(prefix="imu-turn-"))),
            events=events,
        )
        runner.start("s1", "hi")

        seen = []
        deadline = time.time() + 5
        while time.time() < deadline:
            event = subscription.get(timeout=0.5)
            if event is None:
                continue
            seen.append(event.type)
            if event.type == "turn.end":
                break

        self.assertEqual(seen[0], "turn.start")
        self.assertIn("step.start", seen)
        self.assertIn("step", seen)
        self.assertEqual(seen[-1], "turn.end")
        self.assertEqual(seen.count("queue"), 2)


if __name__ == "__main__":
    unittest.main()
