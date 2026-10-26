"""G-5: total load is capped, parallel callers cannot bypass it."""
import threading
import unittest

from services.safety.governor import Governor


class GovernorTests(unittest.TestCase):
    def test_acquire_up_to_capacity(self):
        gov = Governor(10)
        first = gov.acquire(6)
        second = gov.acquire(4)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertEqual(gov.used, 10)

    def test_over_capacity_returns_none(self):
        gov = Governor(10)
        self.assertIsNotNone(gov.acquire(8))
        self.assertIsNone(gov.acquire(3))
        self.assertEqual(gov.used, 8)

    def test_release_frees_capacity(self):
        gov = Governor(10)
        ticket = gov.acquire(10)
        gov.release(ticket)
        self.assertEqual(gov.used, 0)
        self.assertIsNotNone(gov.acquire(10))

    def test_double_release_is_idempotent(self):
        gov = Governor(10)
        ticket = gov.acquire(4)
        gov.release(ticket)
        gov.release(ticket)
        self.assertEqual(gov.used, 0)

    def test_parallel_callers_cannot_exceed_capacity(self):
        gov = Governor(5)
        granted: list[object] = []
        lock = threading.Lock()

        def worker():
            ticket = gov.acquire(1)
            if ticket is not None:
                with lock:
                    granted.append(ticket)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(granted), 5)
        self.assertEqual(gov.used, 5)

    def test_invalid_capacity_rejected(self):
        with self.assertRaises(ValueError):
            Governor(0)


if __name__ == "__main__":
    unittest.main()
