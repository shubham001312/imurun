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


class QueueingModel:
    def __init__(self, queue):
        self.queue = queue
        self.calls = 0

    def generate(self, messages):
        self.calls += 1
        if self.calls == 1:
            self.queue.push("s1", "extra")
        return f"answer-{self.calls}"


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

    def test_a_queued_prompt_and_every_step_reach_the_transcript(self):
        queue = PromptQueue()
        transcripts = TranscriptStore(Path(tempfile.mkdtemp(prefix="imu-turn-")))
        events = EventBroker()
        subscription = events.subscribe()
        runner = TurnRunner(
            QueueingModel(queue),
            queue=queue,
            transcripts=transcripts,
            events=events,
        )
        turn = runner.start("s1", "hi")

        deadline = time.time() + 5
        while time.time() < deadline:
            event = subscription.get(timeout=0.5)
            if event is not None and event.type == "turn.end":
                break

        self.assertEqual(turn.steps, 2)
        messages = transcripts.read("s1")
        self.assertEqual(
            [message.role for message in messages],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(
            [message.content for message in messages],
            ["hi", "answer-1", "extra", "answer-2"],
        )


if __name__ == "__main__":
    unittest.main()
