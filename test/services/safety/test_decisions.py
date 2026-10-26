"""G3/G1/G2/G4: the code decides allow / ask / deny, always with a reason."""
import os
import sys
import tempfile
import unittest

from services.safety import decisions
from services.safety.decisions import (
    Grant,
    ModelRequest,
    PermissionRequired,
    check_action,
    check_request,
    check_start,
    require,
)
from services.safety.limits import limits
from services.safety.profile import GpuInfo, MachineProfile

GB = 1024**3


def machine(available=8 * GB, disk=100 * GB, errors=()):
    return MachineProfile(
        ram_total=16 * GB,
        ram_available=available,
        cpu_cores=8,
        disk_free=disk,
        gpu=GpuInfo(present=False),
        errors=tuple(errors),
    )


class StartTests(unittest.TestCase):
    def setUp(self):
        self.profile = machine()
        self.caps = limits(self.profile, "conservative")

    def test_small_model_is_allowed(self):
        decision = check_start(ModelRequest(model_bytes=1 * GB, context_tokens=2048), self.caps, self.profile)
        self.assertEqual(decision.kind, "allow")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.reasons)

    def test_model_over_budget_asks_for_full_mode(self):
        decision = check_start(ModelRequest(model_bytes=9 * GB, context_tokens=2048), self.caps, self.profile)
        self.assertEqual(decision.kind, "ask")
        self.assertIn("full-resource", decision.reasons[0])

    def test_huge_model_with_disk_room_asks_for_streaming_opt_in(self):
        decision = check_start(ModelRequest(model_bytes=20 * GB, context_tokens=2048), self.caps, self.profile)
        self.assertEqual(decision.kind, "ask")
        self.assertIn("stream from disk", decision.reasons[0])

    def test_not_enough_disk_is_denied(self):
        tight = machine(disk=3 * GB)
        decision = check_start(ModelRequest(model_bytes=2 * GB, context_tokens=2048), limits(tight), tight)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G4-disk")

    def test_failed_probe_asks_instead_of_lying(self):
        broken = machine(errors=("memory: boom",))
        decision = check_start(ModelRequest(model_bytes=1 * GB), limits(broken), broken)
        self.assertEqual(decision.kind, "ask")
        self.assertEqual(decision.rule, "G2-hardware")
        self.assertIn("memory: boom", decision.reasons[0])

    def test_too_many_threads_asks(self):
        decision = check_start(ModelRequest(model_bytes=1 * GB, threads=64), self.caps, self.profile)
        self.assertEqual(decision.kind, "ask")

    def test_kv_cache_is_part_of_the_budget(self):
        decision = check_start(
            ModelRequest(model_bytes=1 * GB, context_tokens=100_000), self.caps, self.profile
        )
        self.assertEqual(decision.kind, "ask")
        self.assertIn("resident need", decision.reasons[0])

    def test_messages_use_human_sizes(self):
        tight = machine(disk=int(2.2 * GB))
        decision = check_start(
            ModelRequest(model_bytes=500 * 1024**2, context_tokens=1024), limits(tight), tight
        )
        self.assertEqual(decision.kind, "deny")
        self.assertIn("0.5 GB", decision.reasons[0])


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

    def test_guardrail_source_is_protected(self):
        path = os.path.join(decisions.GUARDRAIL_DIR, "decisions.py")
        decision = self.check(kind="file_write", path=path)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G3-protected")

    def test_git_directory_is_protected(self):
        path = os.path.join(self.project, ".git", "hooks", "pre-commit")
        self.assertEqual(self.check(kind="file_write", path=path).rule, "G3-protected")

    def test_env_files_are_protected(self):
        for name in (".env", ".env.local"):
            path = os.path.join(self.project, name)
            self.assertEqual(self.check(kind="file_write", path=path).rule, "G3-protected")

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
        commands = (
            "rm -rf /",
            "rm -rf /*",
            "rm -rf --no-preserve-root /",
            "mkfs.ext4 /dev/sda1",
            "format c:",
            "diskpart",
            "Remove-Item -Recurse -Force C:\\",
            "Format-Volume -DriveLetter C",
            "Clear-Disk -Number 0",
            "Stop-Computer",
            "Set-ExecutionPolicy Unrestricted",
        )
        for command in commands:
            self.assertEqual(self.check(kind="shell", command=command).kind, "deny", command)

    def test_network_allowlisted_is_allowed(self):
        decision = check_action(
            {"kind": "network", "host": "duckduckgo.com"},
            project_root=self.project,
            user_data_root=self.data,
            allowed_hosts=("duckduckgo.com",),
        )
        self.assertEqual(decision.kind, "allow")
        self.assertEqual(decision.rule, "G5-network")

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


class GrantTests(unittest.TestCase):
    def setUp(self):
        self.project = tempfile.mkdtemp(prefix="imu-project-")
        self.data = tempfile.mkdtemp(prefix="imu-data-")

    def test_a_tool_outside_the_grant_is_denied(self):
        grant = Grant(tools=frozenset({"file_read"}), roots=(self.project,))
        decision = check_action(
            {"kind": "file_write", "path": os.path.join(self.project, "x.txt")},
            project_root=self.project,
            user_data_root=self.data,
            grant=grant,
        )
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G3-grant")

    def test_a_granted_write_inside_its_roots_is_allowed(self):
        grant = Grant(tools=frozenset({"file_write"}), roots=(self.project,))
        decision = check_action(
            {"kind": "file_write", "path": os.path.join(self.project, "x.txt")},
            project_root=self.project,
            user_data_root=self.data,
            grant=grant,
        )
        self.assertEqual(decision.kind, "allow")

    def test_a_granted_write_outside_its_roots_is_denied(self):
        grant = Grant(tools=frozenset({"file_write"}), roots=(self.project,))
        decision = check_action(
            {"kind": "file_write", "path": os.path.join(self.data, "x.txt")},
            project_root=self.project,
            user_data_root=self.data,
            grant=grant,
        )
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G3-outside-roots")

    def test_intersection_never_escalates(self):
        parent = Grant(tools=frozenset({"file_read", "file_write"}), roots=(self.project,), hosts=frozenset({"a.com"}))
        child = Grant(tools=frozenset({"file_write", "shell"}), roots=(self.project,), hosts=frozenset({"a.com", "b.com"}))
        merged = parent.intersect(child)
        self.assertEqual(merged.tools, frozenset({"file_write"}))
        self.assertEqual(merged.hosts, frozenset({"a.com"}))
        self.assertEqual(len(merged.roots), 1)


class RequireTests(unittest.TestCase):
    def test_allow_passes(self):
        decision = require(type("D", (), {"kind": "allow", "rule": "G3", "reasons": ()})())
        self.assertEqual(decision.kind, "allow")

    def test_ask_without_a_grant_raises(self):
        from services.safety.decisions import Decision

        with self.assertRaises(PermissionRequired):
            require(Decision("ask", "G3-action", ("needs permission",)))

    def test_ask_with_a_grant_passes(self):
        from services.safety.decisions import Decision

        self.assertEqual(require(Decision("ask", "G3-action"), granted=True).kind, "ask")

    def test_deny_always_raises(self):
        from services.safety.decisions import Decision

        with self.assertRaises(PermissionRequired):
            require(Decision("deny", "G3-destructive"), granted=True)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.caps = limits(machine(), "conservative")

    def test_oversized_request_is_denied(self):
        decision = check_request(self.caps.max_request_bytes + 1, 0.0, self.caps)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G1-request")

    def test_timed_out_request_is_denied(self):
        decision = check_request(10, self.caps.request_timeout + 1, self.caps)
        self.assertEqual(decision.kind, "deny")
        self.assertEqual(decision.rule, "G1-timeout")

    def test_normal_request_is_allowed(self):
        self.assertEqual(check_request(10, 1.0, self.caps).kind, "allow")


class InputValidationTests(unittest.TestCase):
    def setUp(self):
        self.profile = machine()
        self.caps = limits(self.profile, "conservative")

    def test_negative_model_bytes_are_denied(self):
        decision = check_start(ModelRequest(model_bytes=-1), self.caps, self.profile)
        self.assertEqual(decision.kind, "deny")

    def test_zero_threads_are_denied(self):
        decision = check_start(ModelRequest(model_bytes=1 * GB, threads=0), self.caps, self.profile)
        self.assertEqual(decision.kind, "deny")

    def test_negative_context_is_denied(self):
        decision = check_start(
            ModelRequest(model_bytes=1 * GB, context_tokens=-1), self.caps, self.profile
        )
        self.assertEqual(decision.kind, "deny")

    def test_negative_request_size_is_denied(self):
        self.assertEqual(check_request(-1, 0.0, self.caps).kind, "deny")

    def test_negative_elapsed_is_denied(self):
        self.assertEqual(check_request(1, -1.0, self.caps).kind, "deny")

    def test_secret_files_are_protected(self):
        project = tempfile.mkdtemp(prefix="imu-project-")
        for name in (".env", ".env.local", ".netrc", ".npmrc", ".pypirc", ".git-credentials"):
            with self.subTest(name=name):
                decision = check_action(
                    {"kind": "file_read", "path": os.path.join(project, name)},
                    project_root=project,
                    user_data_root=project,
                )
                self.assertEqual(decision.kind, "deny")


if __name__ == "__main__":
    unittest.main()
