"""G-7/G-8: marking never throws; the audit tail is readable."""
import unittest

from services.safety.audit import Audit


class AuditTests(unittest.TestCase):
    def test_mark_records_the_event(self):
        audit = Audit()
        event = audit.mark("G1-ram", "model:x", "over budget")
        self.assertEqual(event.rule, "G1-ram")
        self.assertEqual(audit.tail()[0].subject, "model:x")

    def test_sink_receives_every_event(self):
        seen = []
        audit = Audit(sink=seen.append)
        audit.mark("G3-shell", "cmd", "ask")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].rule, "G3-shell")

    def test_a_broken_sink_is_recorded_not_raised(self):
        def broken(_event):
            raise RuntimeError("sink down")

        audit = Audit(sink=broken)
        audit.mark("G2-limits", "caps", "computed")  # must not raise
        rules = [event.rule for event in audit.tail()]
        self.assertIn("G2-limits", rules)
        self.assertIn("G7-sink", rules)

    def test_tail_respects_limit_and_order(self):
        audit = Audit()
        for index in range(5):
            audit.mark("R", f"s{index}", "why")
        tail = audit.tail(2)
        self.assertEqual([event.subject for event in tail], ["s3", "s4"])

    def test_capacity_trims_the_oldest(self):
        audit = Audit(capacity=3)
        for index in range(5):
            audit.mark("R", f"s{index}", "why")
        self.assertEqual([event.subject for event in audit.tail(10)], ["s2", "s3", "s4"])

    def test_empty_tail(self):
        self.assertEqual(Audit().tail(0), [])

    def test_invalid_capacity_rejected(self):
        with self.assertRaises(ValueError):
            Audit(capacity=0)


if __name__ == "__main__":
    unittest.main()
