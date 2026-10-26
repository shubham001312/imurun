"""G-1 probe: real values, and the failure path is marked, not raised."""
import unittest

from services.safety.profile import GpuInfo, probe


class ProbeTests(unittest.TestCase):
    def test_reads_real_machine(self):
        profile = probe()
        self.assertGreater(profile.ram_total, 0)
        self.assertGreaterEqual(profile.ram_available, 0)
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


if __name__ == "__main__":
    unittest.main()
