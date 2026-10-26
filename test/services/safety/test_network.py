"""Batch G: loopback by default, grants expire, connector hosts are pinned."""
import unittest

from services.safety.network import NetworkDenied, NetworkGuard, host_of


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class NetworkGuardTests(unittest.TestCase):
    def setUp(self):
        self.marks: list[tuple[str, str, str]] = []
        self.clock = FakeClock()
        self.guard = NetworkGuard(
            mark=lambda rule, subject, why: self.marks.append((rule, subject, why)),
            clock=self.clock,
        )

    def test_loopback_is_allowed(self):
        for url in ("http://127.0.0.1:8080/v1", "https://localhost/health", "http://[::1]:9000"):
            with self.subTest(url=url):
                self.guard.check(url)

    def test_external_host_is_denied_without_a_grant(self):
        with self.assertRaises(NetworkDenied):
            self.guard.check("https://api.github.com/user")
        self.assertEqual(self.marks[-1][0], "G5-network")
        self.assertEqual(self.marks[-1][1], "api.github.com")
        self.assertIn("denied", self.marks[-1][2])

    def test_a_grant_allows_exactly_one_host(self):
        self.guard.grant("api.github.com", ttl=60)
        self.guard.check("https://api.github.com/user")
        with self.assertRaises(NetworkDenied):
            self.guard.check("https://api.gitlab.com/user")

    def test_a_grant_expires(self):
        self.guard.grant("api.github.com", ttl=10)
        self.clock.now += 11
        with self.assertRaises(NetworkDenied):
            self.guard.check("https://api.github.com/user")

    def test_a_grant_can_be_revoked(self):
        self.guard.grant("api.github.com", ttl=60)
        self.guard.revoke("api.github.com")
        with self.assertRaises(NetworkDenied):
            self.guard.check("https://api.github.com/user")

    def test_only_http_and_https_are_allowed(self):
        for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://example.com"):
            with self.subTest(url=url):
                with self.assertRaises(NetworkDenied):
                    host_of(url)

    def test_a_url_without_a_host_is_denied(self):
        with self.assertRaises(NetworkDenied):
            host_of("http:///path")

    def test_pins_confine_a_provider_to_its_hosts(self):
        self.guard.pin("github", ("api.github.com",))
        self.guard.check_for("github", "https://api.github.com/user")
        with self.assertRaises(NetworkDenied):
            self.guard.check_for("github", "https://evil.example/steal")

    def test_another_provider_cannot_use_the_pin(self):
        self.guard.pin("github", ("api.github.com",))
        with self.assertRaises(NetworkDenied):
            self.guard.check_for("slack", "https://api.github.com/user")

    def test_an_unpinned_provider_is_denied(self):
        with self.assertRaises(NetworkDenied):
            self.guard.check_for("github", "https://api.github.com/user")

    def test_grant_and_pin_validation(self):
        with self.assertRaises(ValueError):
            self.guard.grant("")
        with self.assertRaises(ValueError):
            self.guard.grant("api.github.com", ttl=0)
        with self.assertRaises(ValueError):
            self.guard.pin("github", ())

    def test_a_broken_mark_does_not_change_the_answer(self):
        def broken(_rule, _subject, _why):
            raise RuntimeError("sink down")

        guard = NetworkGuard(mark=broken)
        with self.assertRaises(NetworkDenied):
            guard.check("https://api.github.com/user")


if __name__ == "__main__":
    unittest.main()
