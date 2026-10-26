"""G-3/G-4: the code decides allow / ask / deny, always with a reason."""
import os
import sys
import tempfile
import unittest

from services.safety.decisions import ModelRequest, check_action, check_start
from services.safety.limits import limits
from services.safety.profile import GpuInfo, MachineProfile

GB = 1024**3


def machine(available=8 * GB, disk=100 * GB):
    return MachineProfile(
        ram_total=16 * GB,
        ram_available=available,
        cpu_cores=8,
        disk_free=disk,
        gpu=GpuInfo(present=False),
    )


class StartTests(unittest.TestCase):
    def setUp(self):
        self.profile = machine()
        self.caps = limits(self.profile, "conservative")

    def test_small_model_is_allowed(self):
        decision = check_start(ModelRequest(model_bytes=2 * GB), self.caps, self.profile)
        self.assertEqual(decision.kind, "allow")
        self.assertTrue(decision.reasons)

    def test_model_over_budget_asks_for_full_mode(self):
        decision = check_start(ModelRequest(model_bytes=int(8 * GB * 0.7)), self.caps, self.profile)
        self.assertEqual(decision.kind, "ask")
        self.assertIn("full-resource", decision.reasons[0])

    def test_model_far_over_budget_is_denied(self):
        decision = check_start(ModelRequest(model_bytes=10 * GB), self.caps, self.profile)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G1-ram")

    def test_not_enough_disk_is_denied(self):
        tight = machine(disk=3 * GB)
        decision = check_start(ModelRequest(model_bytes=2 * GB), limits(tight, "conservative"), tight)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G4-disk")

    def test_too_many_threads_asks(self):
        decision = check_start(ModelRequest(model_bytes=1 * GB, threads=64), self.caps, self.profile)
        self.assertEqual(decision.kind, "ask")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="imu-project-")
        self.data = tempfile.mkdtemp(prefix="imu-data-")
        self.outside = tempfile.mkdtemp(prefix="imu-outside-")

    def check(self, **action):
        return check_action(action, project_root=self.project, user_data_root=self.data)

    def test_write_inside_project_is_allowed(self):
        path = os.path.join(self.project, "src", "main.py")
        self.assertEqual(self.check(kind="file_write", path=path).kind, "allow")

    def test_write_inside_user_data_is_allowed(self):
        path = os.path.join(self.data, "notes.md")
        self.assertEqual(self.check(kind="file_write", path=path).kind, "allow")

    def test_delete_inside_project_asks(self):
        path = os.path.join(self.project, "old.txt")
        self.assertEqual(self.check(kind="file_delete", path=path).kind, "ask")

    def test_write_outside_roots_is_denied(self):
        path = os.path.join(self.outside, "evil.txt")
        decision = self.check(kind="file_write", path=path)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G3-outside-roots")

    def test_traversal_out_of_project_is_denied(self):
        path = os.path.join(self.project, "..", os.path.basename(self.outside), "evil.txt")
        self.assertEqual(self.check(kind="file_write", path=path).kind, "deny")

    def test_system_path_is_denied(self):
        system = "C:\\Windows\\System32\\drivers\\etc\\hosts" if sys.platform == "win32" else "/etc/passwd"
        decision = self.check(kind="file_write", path=system)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G3-system-path")

    def test_normal_shell_asks(self):
        self.assertEqual(self.check(kind="shell", command="npm test").kind, "ask")

    def test_destructive_shell_is_denied(self):
        for command in ("rm -rf /", "mkfs.ext4 /dev/sda1", "format c:", "diskpart"):
            self.assertEqual(self.check(kind="shell", command=command).kind, "deny", command)

    def test_network_allowlisted_is_allowed(self):
        decision = check_action(
            {"kind": "network", "host": "duckduckgo.com"},
            project_root=self.project,
            user_data_root=self.data,
            allowed_hosts=("duckduckgo.com",),
        )
        self.assertEqual(decision.kind, "allow")

    def test_unknown_network_host_asks(self):
        self.assertEqual(self.check(kind="network", host="example.com").kind, "ask")

    def test_registry_and_device_are_denied(self):
        self.assertEqual(self.check(kind="registry", path="HKLM\\Software").kind, "deny")
        self.assertEqual(self.check(kind="device", path="\\\\.\\PhysicalDrive0").kind, "deny")

    def test_unknown_kind_is_denied_with_reason(self):
        decision = self.check(kind="teleport")
        self.assertEqual(decision.kind, "deny")
        self.assertIn("unknown", decision.reasons[0])

    def test_missing_path_is_denied_with_reason(self):
        decision = self.check(kind="file_write")
        self.assertEqual(decision.kind, "deny")
        self.assertTrue(decision.reasons)


if __name__ == "__main__":
    unittest.main()
