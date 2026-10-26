"""G-6: one command stops everything; failures are marked, not skipped."""
import unittest

from services.safety.killswitch import KillSwitch


class KillSwitchTests(unittest.TestCase):
    def test_fire_calls_every_stop(self):
        called: list[str] = []
        switch = KillSwitch()
        switch.register("engine", lambda: called.append("engine"))
        switch.register("queue", lambda: called.append("queue"))
        results = switch.fire()
        self.assertEqual(sorted(called), ["engine", "queue"])
        self.assertTrue(all(r.ok for r in results))

    def test_a_failing_stop_is_marked_and_others_still_run(self):
        marks: list[tuple[str, str, str]] = []
        called: list[str] = []

        def broken():
            raise RuntimeError("engine stuck")

        switch = KillSwitch(mark=lambda rule, subject, why: marks.append((rule, subject, why)))
        switch.register("engine", broken)
        switch.register("queue", lambda: called.append("queue"))
        results = switch.fire()
        self.assertIn("queue", called)
        engine = next(r for r in results if r.name == "engine")
        self.assertFalse(engine.ok)
        self.assertIn("engine stuck", engine.error or "")
        self.assertEqual(marks[0][0], "G6-killswitch")
        self.assertEqual(marks[0][1], "engine")

    def test_unregister_removes_a_stop(self):
        called: list[str] = []
        switch = KillSwitch()
        switch.register("engine", lambda: called.append("engine"))
        switch.unregister("engine")
        switch.fire()
        self.assertEqual(called, [])

    def test_a_broken_mark_does_not_break_the_kill(self):
        called: list[str] = []

        def broken_mark(_rule, _subject, _why):
            raise RuntimeError("sink down")

        switch = KillSwitch(mark=broken_mark)
        switch.register("engine", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        switch.register("queue", lambda: called.append("queue"))
        results = switch.fire()
        self.assertIn("queue", called)
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
