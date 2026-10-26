"""G-2 probe: real values, injected failures, timeout, model volume."""
import os
import tempfile
import time
import unittest

from services.safety.profile import GpuInfo, probe


class ProbeTests(unittest.TestCase):
    def test_reads_real_machine(self):
        profile = probe()
        # A restricted machine may legitimately fail a part; the contract is
        # "value or a marked error", never a crash and never a silent zero.
        self.assertTrue(profile.ram_total > 0 or any(e.startswith("memory:") for e in profile.errors))
        self.assertGreaterEqual(profile.cpu_cores, 1)
        self.assertGreaterEqual(profile.disk_free, 0)

    def test_failing_memory_is_marked_not_raised(self):
        def broken():
            raise OSError("no memory info")

        profile = probe(memory=broken)
        self.assertEqual(profile.ram_total, 0)
        self.assertTrue(any(e.startswith("memory:") for e in profile.errors))

    def test_failing_disk_is_marked_not_raised(self):
        def broken(_path):
            raise OSError("no disk info")

        profile = probe(disk=broken)
        self.assertEqual(profile.disk_free, 0)
        self.assertTrue(any(e.startswith("disk:") for e in profile.errors))

    def test_failing_gpu_is_marked_not_raised(self):
        def broken():
            raise RuntimeError("no gpu info")

        profile = probe(gpu=broken)
        self.assertFalse(profile.gpu.present)
        self.assertTrue(any(e.startswith("gpu:") for e in profile.errors))

    def test_injected_parts_are_used(self):
        profile = probe(
            memory=lambda: (16 * 1024**3, 8 * 1024**3),
            disk=lambda _path: type("U", (), {"free": 100 * 1024**3})(),
            gpu=lambda: GpuInfo(present=True, vendor="test"),
        )
        self.assertEqual(profile.ram_total, 16 * 1024**3)
        self.assertEqual(profile.ram_available, 8 * 1024**3)
        self.assertEqual(profile.disk_free, 100 * 1024**3)
        self.assertTrue(profile.gpu.present)
        self.assertEqual(profile.errors, ())

    def test_slow_part_hits_the_overall_deadline(self):
        def slow():
            time.sleep(1.0)
            return GpuInfo(present=True, vendor="slow")

        started = time.monotonic()
        profile = probe(gpu=slow, deadline=0.1)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.8, "probe must give up at the deadline")
        self.assertFalse(profile.gpu.present)
        self.assertTrue(any(e.startswith("gpu:") and "timed out" in e for e in profile.errors))

    def test_disk_is_measured_on_the_model_volume(self):
        seen: list[str] = []
        model_dir = tempfile.mkdtemp(prefix="imu-models-")

        def disk(path):
            seen.append(path)
            return type("U", (), {"free": 1 * 1024**3})()

        profile = probe(disk=disk, model_dir=model_dir)
        self.assertEqual(profile.disk_free, 1 * 1024**3)
        self.assertEqual(os.path.normcase(seen[0]), os.path.normcase(os.path.realpath(model_dir)))

    def test_physical_cores_are_at_least_one(self):
        self.assertGreaterEqual(probe(deadline=5.0).cpu_cores, 1)


if __name__ == "__main__":
    unittest.main()
