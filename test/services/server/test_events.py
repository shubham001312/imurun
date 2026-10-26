"""The SSE broker: every subscriber gets every event, slow readers are marked."""
import unittest

from services.server.events import CLOSE, EventBroker


class EventBrokerTests(unittest.TestCase):
    def test_publish_reaches_every_subscriber(self):
        broker = EventBroker()
        first = broker.subscribe()
        second = broker.subscribe()
        self.assertEqual(broker.publish("turn.start", {"turn_id": "t1"}), 2)
        for subscription in (first, second):
            event = subscription.get(timeout=0.1)
            self.assertEqual(event.type, "turn.start")
            self.assertEqual(event.data, {"turn_id": "t1"})

    def test_a_slow_subscriber_drops_the_oldest_and_is_marked(self):
        marks = []
        broker = EventBroker(max_queue=1, mark=lambda *args: marks.append(args))
        subscription = broker.subscribe()
        broker.publish("one", {"n": 1})
        broker.publish("two", {"n": 2})
        self.assertEqual(subscription.dropped, 1)
        self.assertTrue(any("dropped 1 event" in args[2] for args in marks))
        event = subscription.get(timeout=0.1)
        self.assertEqual(event.type, "two")

    def test_close_wakes_a_waiting_reader(self):
        broker = EventBroker()
        subscription = broker.subscribe()
        broker.close()
        self.assertEqual(subscription.get(timeout=0.1).type, CLOSE)

    def test_unsubscribe_removes_a_subscriber(self):
        broker = EventBroker()
        subscription = broker.subscribe()
        broker.unsubscribe(subscription)
        self.assertEqual(broker.publish("x", {}), 0)

    def test_an_empty_event_type_is_refused(self):
        with self.assertRaises(ValueError):
            EventBroker().publish("", {})

    def test_the_queue_must_be_positive(self):
        with self.assertRaises(ValueError):
            EventBroker(max_queue=0).subscribe()


if __name__ == "__main__":
    unittest.main()
