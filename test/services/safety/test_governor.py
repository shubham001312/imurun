"""G1: FIFO queue, per-client caps, crash-safe leases, real units."""
import threading
import time
import unittest

from services.safety.governor import Governor


def wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class GovernorTests(unittest.TestCase):
    def test_acquire_up_to_capacity(self):
        gov = Governor(10)
        first = gov.acquire(6, timeout=0.1)
        second = gov.acquire(4, timeout=0.1)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(gov.status()["used"], 10)

    def test_over_capacity_returns_none(self):
        gov = Governor(10)
        self.assertIsNotNone(gov.acquire(8, timeout=0.1))
        self.assertIsNone(gov.acquire(3, timeout=0.1))
        self.assertEqual(gov.status()["used"], 8)

    def test_release_frees_capacity(self):
        gov = Governor(10)
        ticket = gov.acquire(10, timeout=0.1)
        gov.release(ticket)
        self.assertEqual(gov.status()["used"], 0)
        self.assertIsNotNone(gov.acquire(10, timeout=0.1))

    def test_double_release_is_idempotent(self):
        gov = Governor(10)
        ticket = gov.acquire(4, timeout=0.1)
        gov.release(ticket)
        gov.release(ticket)
        self.assertEqual(gov.status()["used"], 0)

    def test_waiters_are_served_in_arrival_order(self):
        gov = Governor(1)
        first = gov.acquire(1, client="a", timeout=0.1)
        order: list[str] = []

        def worker(client):
            ticket = gov.acquire(1, client=client, timeout=3.0)
            if ticket is not None:
                order.append(client)
                time.sleep(0.05)
                gov.release(ticket)

        second = threading.Thread(target=worker, args=("b",))
        third = threading.Thread(target=worker, args=("c",))
        second.start()
        self.assertTrue(wait_until(lambda: gov.status()["queued"] == ["b"]))
        third.start()
        self.assertTrue(wait_until(lambda: gov.status()["queued"] == ["b", "c"]))
        gov.release(first)
        second.join(3)
        third.join(3)
        self.assertEqual(order, ["b", "c"])

    def test_per_client_cap_stops_one_session_starving_the_rest(self):
        gov = Governor(10, per_client=4)
        self.assertIsNotNone(gov.acquire(4, client="a", timeout=0.1))
        self.assertIsNone(gov.acquire(1, client="a", timeout=0.1))
        self.assertIsNotNone(gov.acquire(4, client="b", timeout=0.1))

    def test_a_crashed_caller_lease_is_reclaimed_and_marked(self):
        marks: list[tuple[str, str, str]] = []
        gov = Governor(1, lease_seconds=0.05, mark=lambda rule, subject, why: marks.append((rule, subject, why)))
        gov.acquire(1, client="crashed", timeout=0.1)
        time.sleep(0.1)
        ticket = gov.acquire(1, client="healthy", timeout=1.0)
        self.assertIsNotNone(ticket)
        self.assertEqual(marks[0][0], "G1-lease")
        self.assertEqual(marks[0][1], "crashed")

    def test_parallel_callers_cannot_exceed_capacity(self):
        gov = Governor(5)
        granted: list[object] = []
        lock = threading.Lock()

        def worker():
            ticket = gov.acquire(1, client="worker", timeout=0.4)
            if ticket is not None:
                with lock:
                    granted.append(ticket)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(granted), 5)
        self.assertEqual(gov.status()["used"], 5)

    def test_status_reports_capacity_used_and_queue(self):
        gov = Governor(4, per_client=3)
        gov.acquire(3, client="a", timeout=0.1)
        waiting = threading.Thread(target=lambda: gov.acquire(2, client="b", timeout=1.0))
        waiting.start()
        self.assertTrue(wait_until(lambda: gov.status()["queued"] == ["b"]))
        status = gov.status()
        self.assertEqual(status["capacity"], 4)
        self.assertEqual(status["used"], 3)
        self.assertEqual(status["per_client"], 3)
        waiting.join(2)

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(ValueError):
            Governor(0)
        with self.assertRaises(ValueError):
            Governor(10, per_client=0)
        with self.assertRaises(ValueError):
            Governor(10, lease_seconds=0)
        gov = Governor(10)
        with self.assertRaises(ValueError):
            gov.acquire(0)


if __name__ == "__main__":
    unittest.main()
