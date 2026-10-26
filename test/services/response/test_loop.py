"""V25: queued prompts at step boundaries, kill switch, persona, max steps."""
import unittest

from services.prompts.queue import PromptQueue
from services.response.loop import AgentLoop
from services.safety.audit import Audit
from services.safety.killswitch import KillSwitch


class FakeModel:
    def __init__(self, queue=None, kill=None, push_at=(), kill_at=None, session_id="s1"):
        self.queue = queue
        self.kill = kill
        self.push_at = set(push_at)
        self.kill_at = kill_at
        self.session_id = session_id
        self.calls = 0
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.calls += 1
        self.seen.append([dict(message) for message in messages])
        if self.kill is not None and self.kill_at == self.calls:
            self.kill.fire()
        if self.queue is not None and self.calls in self.push_at:
            self.queue.push(self.session_id, f"queued-{self.calls}")
        return f"answer-{self.calls}"


class FakePersona:
    def directive(self):
        return "be brief and honest"


class LoopTests(unittest.TestCase):
    def setUp(self):
        self.queue = PromptQueue()
        self.audit = Audit()

    def test_one_prompt_one_step(self):
        model = FakeModel()
        result = AgentLoop(model, queue=self.queue, audit=self.audit, session_id="s1").run("hi")
        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.answer, "answer-1")

    def test_a_prompt_queued_before_the_run_is_delivered_first(self):
        self.queue.push("s1", "extra context")
        model = FakeModel()
        AgentLoop(model, queue=self.queue, audit=self.audit, session_id="s1").run("hi")
        contents = [message["content"] for message in model.seen[0]]
        self.assertIn("extra context", contents)
        self.assertIn("hi", contents)

    def test_a_prompt_queued_during_generation_gets_another_step(self):
        model = FakeModel(queue=self.queue, push_at={1})
        result = AgentLoop(model, queue=self.queue, audit=self.audit, session_id="s1").run("hi")
        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.steps), 2)
        contents = [message["content"] for message in model.seen[1]]
        self.assertIn("queued-1", contents)

    def test_the_kill_switch_stops_the_loop(self):
        kill = KillSwitch()
        model = FakeModel(kill=kill, kill_at=1)
        result = AgentLoop(model, queue=self.queue, audit=self.audit, kill=kill, session_id="s1").run("hi")
        self.assertEqual(result.status, "stopped")
        self.assertTrue(any(event.rule == "G6-killswitch" for event in self.audit.tail(10)))

    def test_max_steps_is_marked(self):
        model = FakeModel(queue=self.queue, push_at=range(1, 10))
        result = AgentLoop(
            model, queue=self.queue, audit=self.audit, max_steps=3, session_id="s1"
        ).run("hi")
        self.assertEqual(result.status, "max-steps")
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(any(event.rule == "G1-loop" for event in self.audit.tail(10)))

    def test_persona_directive_is_a_system_message(self):
        model = FakeModel()
        AgentLoop(
            model, queue=self.queue, audit=self.audit, persona=FakePersona(), session_id="s1"
        ).run("hi")
        self.assertEqual(model.seen[0][0], {"role": "system", "content": "be brief and honest"})

    def test_delivery_is_marked(self):
        self.queue.push("s1", "extra")
        model = FakeModel()
        AgentLoop(model, queue=self.queue, audit=self.audit, session_id="s1").run("hi")
        events = [event for event in self.audit.tail(20) if event.rule == "G1-prompt"]
        self.assertTrue(any("delivered" in event.why for event in events))

    def test_an_empty_prompt_is_refused(self):
        with self.assertRaises(ValueError):
            AgentLoop(FakeModel(), queue=self.queue).run("   ")

    def test_max_steps_must_be_positive(self):
        with self.assertRaises(ValueError):
            AgentLoop(FakeModel(), max_steps=0)

    def test_close_detaches_the_loop_from_the_kill_switch(self):
        kill = KillSwitch()
        loop = AgentLoop(
            FakeModel(), queue=self.queue, audit=self.audit, kill=kill, session_id="s1"
        )
        loop.run("hi")
        loop.close()
        self.assertEqual(kill.fire(), [])
        loop.close()  # closing twice is safe


if __name__ == "__main__":
    unittest.main()
