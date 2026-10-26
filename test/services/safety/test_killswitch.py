"""G6: one command stops everything; duplicates all fire; every stop is marked."""
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

    def test_duplicate_names_both_fire(self):
        called: list[str] = []
        switch = KillSwitch()
        first = switch.register("stream", lambda: called.append("first"))
        second = switch.register("stream", lambda: called.append("second"))
        self.assertNotEqual(first, second)
        results = switch.fire()
        self.assertEqual(sorted(called), ["first", "second"])
        self.assertEqual(len(results), 2)

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
        failures = [mark for mark in marks if mark[0] == "G6-killswitch" and "engine" == mark[1]]
        self.assertTrue(any("stuck" in mark[2] for mark in failures))

    def test_successful_stops_are_marked_too(self):
        marks: list[tuple[str, str, str]] = []
        switch = KillSwitch(mark=lambda rule, subject, why: marks.append((rule, subject, why)))
        switch.register("queue", lambda: None)
        switch.fire()
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0], ("G6-killswitch", "queue", "stopped"))

    def test_unregister_removes_only_that_id(self):
        called: list[str] = []
        switch = KillSwitch()
        first = switch.register("stream", lambda: called.append("first"))
        switch.register("stream", lambda: called.append("second"))
        switch.unregister(first)
        switch.fire()
        self.assertEqual(called, ["second"])

    def test_a_non_callable_is_rejected_at_register_time(self):
        switch = KillSwitch()
        with self.assertRaises(TypeError):
            switch.register("broken", "not a function")
        self.assertEqual(switch.fire(), [])

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
