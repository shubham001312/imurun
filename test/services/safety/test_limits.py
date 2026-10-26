"""G-2 limits: conservative by default, full only when asked."""
import unittest

from services.safety.limits import limits
from services.safety.profile import GpuInfo, MachineProfile

GB = 1024**3


def profile(available=8 * GB, cores=8):
    return MachineProfile(
        ram_total=16 * GB,
        ram_available=available,
        cpu_cores=cores,
        disk_free=100 * GB,
        gpu=GpuInfo(present=False),
    )


class LimitsTests(unittest.TestCase):
    def test_conservative_uses_sixty_percent(self):
        caps = limits(profile(), "conservative")
        self.assertEqual(caps.max_model_ram, int(8 * GB * 0.60))
        self.assertEqual(caps.max_threads, 7)
        self.assertEqual(caps.min_free_disk, 2 * GB)

    def test_full_uses_ninety_percent_and_all_cores(self):
        caps = limits(profile(), "full")
        self.assertEqual(caps.max_model_ram, int(8 * GB * 0.90))
        self.assertEqual(caps.max_threads, 8)
        self.assertEqual(caps.min_free_disk, 1 * GB)

    def test_default_is_conservative(self):
        self.assertEqual(limits(profile()).mode, "conservative")

    def test_threads_never_zero(self):
        self.assertEqual(limits(profile(cores=1), "conservative").max_threads, 1)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            limits(profile(), "reckless")


if __name__ == "__main__":
    unittest.main()
