"""G-1 limits: stable total-RAM budget, KV/overhead accounted, timeout present."""
import unittest

from services.safety.limits import (
    COMPUTE_OVERHEAD,
    KV_BYTES_PER_TOKEN,
    limits,
)
from services.safety.profile import GpuInfo, MachineProfile

GB = 1024**3


def profile(total=16 * GB, available=8 * GB, cores=8):
    return MachineProfile(
        ram_total=total,
        ram_available=available,
        cpu_cores=cores,
        disk_free=100 * GB,
        gpu=GpuInfo(present=False),
    )


class LimitsTests(unittest.TestCase):
    def test_conservative_uses_total_ram_not_transient_available(self):
        caps = limits(profile(), "conservative")
        self.assertEqual(caps.max_model_ram, int(16 * GB * 0.60))
        self.assertEqual(caps.max_threads, 7)
        self.assertEqual(caps.min_free_disk, 2 * GB)

    def test_full_uses_ninety_percent_and_all_cores(self):
        caps = limits(profile(), "full")
        self.assertEqual(caps.max_model_ram, int(16 * GB * 0.90))
        self.assertEqual(caps.max_threads, 8)
        self.assertEqual(caps.min_free_disk, 1 * GB)

    def test_default_is_conservative(self):
        self.assertEqual(limits(profile()).mode, "conservative")

    def test_threads_never_zero(self):
        self.assertEqual(limits(profile(cores=1), "conservative").max_threads, 1)

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            limits(profile(), "reckless")

    def test_request_size_and_timeout_contract_exists(self):
        caps = limits(profile(), "conservative")
        self.assertGreater(caps.max_request_bytes, 0)
        self.assertGreater(caps.request_timeout, 0)
        self.assertEqual(limits(profile(), "full").request_timeout, 300.0)

    def test_kv_cache_and_compute_overhead_are_budgeted(self):
        caps = limits(profile(), "conservative")
        self.assertEqual(caps.kv_bytes_per_token, KV_BYTES_PER_TOKEN)
        self.assertEqual(caps.compute_overhead, COMPUTE_OVERHEAD)
        self.assertGreater(caps.kv_bytes_per_token, 0)

    def test_falls_back_to_available_when_total_is_unknown(self):
        caps = limits(profile(total=0, available=8 * GB), "conservative")
        self.assertEqual(caps.max_model_ram, int(8 * GB * 0.60))


if __name__ == "__main__":
    unittest.main()
