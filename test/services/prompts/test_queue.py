"""D2: queued prompts reach the model mid-task and before the task ends."""
import threading
import time
import unittest

from services.prompts.queue import PromptQueue


class PromptQueueTests(unittest.TestCase):
    def test_push_then_drain_in_arrival_order(self):
        queue = PromptQueue()
        queue.push("s1", "first")
        queue.push("s1", "second")
        delivered = queue.drain("s1")
        self.assertEqual([item.text for item in delivered], ["first", "second"])
        self.assertTrue(all(item.delivered for item in delivered))

    def test_pending_does_not_consume(self):
        queue = PromptQueue()
        queue.push("s1", "hello")
        self.assertEqual([item.text for item in queue.pending("s1")], ["hello"])
        self.assertTrue(queue.has_pending("s1"))
        self.assertEqual([item.text for item in queue.pending("s1")], ["hello"])

    def test_sessions_are_isolated(self):
        queue = PromptQueue()
        queue.push("parent", "for parent")
        queue.push("child", "for child")
        self.assertEqual([item.text for item in queue.drain("parent")], ["for parent"])
        self.assertTrue(queue.has_pending("child"))
        self.assertFalse(queue.has_pending("parent"))

    def test_cancel_undelivered(self):
        queue = PromptQueue()
        prompt = queue.push("s1", "typo")
        self.assertTrue(queue.cancel(prompt.id))
        self.assertFalse(queue.has_pending("s1"))
        self.assertFalse(queue.cancel(prompt.id))

    def test_cancel_after_delivery_is_refused(self):
        queue = PromptQueue()
        prompt = queue.push("s1", "sent")
        queue.drain("s1")
        self.assertFalse(queue.cancel(prompt.id))

    def test_wait_wakes_on_push(self):
        queue = PromptQueue()
        box: list[object] = []

        def waiter():
            box.append(queue.wait("s1", timeout=2.0))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.05)
        queue.push("s1", "now")
        thread.join(2)
        self.assertIsNotNone(box[0])
        self.assertEqual(box[0].text, "now")

    def test_wait_times_out(self):
        queue = PromptQueue()
        started = time.monotonic()
        self.assertIsNone(queue.wait("s1", timeout=0.1))
        self.assertLess(time.monotonic() - started, 1.0)

    def test_text_is_kept_verbatim(self):
        queue = PromptQueue()
        text = "line one\nline two - ünïcode"
        queue.push("s1", text)
        self.assertEqual(queue.drain("s1")[0].text, text)

    def test_validation(self):
        queue = PromptQueue()
        with self.assertRaises(ValueError):
            queue.push("", "x")
        with self.assertRaises(ValueError):
            queue.push("s1", "")

    def test_history_keeps_delivered_items(self):
        queue = PromptQueue()
        queue.push("s1", "a")
        queue.drain("s1")
        history = queue.history("s1")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].delivered)


class PromptSizeTests(unittest.TestCase):
    def test_an_oversized_prompt_is_refused(self):
        queue = PromptQueue(max_text=10)
        with self.assertRaises(ValueError):
            queue.push("s1", "x" * 11)

    def test_a_prompt_at_the_limit_is_accepted(self):
        queue = PromptQueue(max_text=10)
        self.assertEqual(queue.push("s1", "x" * 10).text, "x" * 10)

    def test_max_text_must_be_positive(self):
        with self.assertRaises(ValueError):
            PromptQueue(max_text=0)


class CancelledHistoryTests(unittest.TestCase):
    def test_a_cancelled_prompt_is_kept_in_history_and_marked(self):
        queue = PromptQueue()
        prompt = queue.push("s1", "stop")
        marks = []
        self.assertTrue(
            queue.cancel(prompt.id, mark=lambda rule, subject, why: marks.append((rule, subject, why)))
        )
        history = queue.history("s1")
        self.assertEqual(len(history), 1)
        self.assertTrue(history[0].cancelled)
        self.assertEqual(marks[0][0], "G1-prompt")


if __name__ == "__main__":
    unittest.main()
