"""GitHub and Gmail adapters: real request shapes, gated writes."""
import unittest

from services.connectors.base import ConnectorError
from services.connectors.gate import ConnectorGate
from services.connectors.providers.canva import CanvaConnector
from services.connectors.providers.cloudflare import CloudflareConnector
from services.connectors.providers.github import GitHubConnector
from services.connectors.providers.gmail import GmailConnector
from services.connectors.providers.render import RenderConnector
from services.connectors.providers.slack import SlackConnector
from services.connectors.providers.supabase import SupabaseConnector
from services.connectors.providers.vercel import VercelConnector
from services.connectors.tokens import MemoryTokenStore
from services.safety.decisions import PermissionRequired


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def request(self, method, url, headers, json=None):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "json": json})
        return {"ok": True}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tokens = MemoryTokenStore()
        self.transport = FakeTransport()
        self.marks: list[tuple[str, str, str]] = []
        self.gate = ConnectorGate(mark=lambda rule, subject, why: self.marks.append((rule, subject, why)))

    def test_github_requires_a_token(self):
        github = GitHubConnector(self.transport, self.tokens)
        self.assertFalse(github.connected())
        with self.assertRaises(Exception):
            self.gate.execute(github, "list_repos", {})

    def test_github_read_uses_the_token(self):
        self.tokens.set("github", "gh-token-1234")
        github = GitHubConnector(self.transport, self.tokens)
        result = self.gate.execute(github, "list_repos", {})
        self.assertTrue(result["ok"])
        call = self.transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://api.github.com/user/repos")
        self.assertIn("gh-token-1234", call["headers"]["Authorization"])

    def test_github_write_needs_approval(self):
        self.tokens.set("github", "gh-token-1234")
        github = GitHubConnector(self.transport, self.tokens)
        with self.assertRaises(PermissionRequired):
            self.gate.execute(github, "create_issue", {"repo": "a/b", "title": "Bug"})
        self.assertEqual(self.transport.calls, [])

    def test_github_issue_is_created_when_approved(self):
        self.tokens.set("github", "gh-token-1234")
        github = GitHubConnector(self.transport, self.tokens)
        self.gate.execute(
            github,
            "create_issue",
            {"repo": "a/b", "title": "Bug", "body": "details"},
            approved=True,
        )
        call = self.transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["url"], "https://api.github.com/repos/a/b/issues")
        self.assertEqual(call["json"], {"title": "Bug", "body": "details"})

    def test_gmail_read_is_allowed(self):
        self.tokens.set("gmail", "gm-token")
        gmail = GmailConnector(self.transport, self.tokens)
        self.gate.execute(gmail, "list_messages", {"q": "is:unread"})
        self.assertIn("q=is%3Aunread", self.transport.calls[0]["url"])

    def test_gmail_send_needs_approval(self):
        self.tokens.set("gmail", "gm-token")
        gmail = GmailConnector(self.transport, self.tokens)
        with self.assertRaises(PermissionRequired):
            self.gate.execute(gmail, "send", {"raw": "cmF3"})
        self.assertEqual(self.transport.calls, [])

    def test_gmail_send_runs_when_approved(self):
        self.tokens.set("gmail", "gm-token")
        gmail = GmailConnector(self.transport, self.tokens)
        self.gate.execute(gmail, "send", {"raw": "cmF3"}, approved=True)
        call = self.transport.calls[0]
        self.assertEqual(call["url"], "https://gmail.googleapis.com/gmail/v1/users/me/messages/send")
        self.assertEqual(call["json"], {"raw": "cmF3"})

    def test_gmail_draft_is_a_write(self):
        self.tokens.set("gmail", "gm-token")
        gmail = GmailConnector(self.transport, self.tokens)
        self.assertEqual(self.gate.check(gmail, "create_draft").kind, "ask")

    def test_tokens_never_appear_in_the_audit(self):
        self.tokens.set("github", "gh-token-1234")
        self.tokens.set("gmail", "gm-token-5678")
        github = GitHubConnector(self.transport, self.tokens)
        gmail = GmailConnector(self.transport, self.tokens)
        self.gate.execute(github, "list_repos", {})
        self.gate.execute(gmail, "send", {"raw": "cmF3"}, approved=True)
        joined = " ".join(mark[2] for mark in self.marks)
        self.assertNotIn("gh-token-1234", joined)
        self.assertNotIn("gm-token-5678", joined)


class ProviderInjectionTests(unittest.TestCase):
    def setUp(self):
        self.tokens = MemoryTokenStore()
        self.transport = FakeTransport()
        self.gate = ConnectorGate()

    def test_url_path_and_query_injection_is_refused(self):
        cases = [
            ("github", GitHubConnector, "list_issues", {"repo": "a/../../b"}),
            (
                "github",
                GitHubConnector,
                "comment",
                {"repo": "a/b", "number": "1/../../x", "body": "hi"},
            ),
            ("gmail", GmailConnector, "get_message", {"id": "../x"}),
            ("slack", SlackConnector, "get_history", {"channel": "C1&x=1"}),
            ("vercel", VercelConnector, "list_deployments", {"project": "p?x=1"}),
            ("render", RenderConnector, "list_deploys", {"service": "s/../x"}),
            ("cloudflare", CloudflareConnector, "list_dns_records", {"zone": "z/../x"}),
            ("canva", CanvaConnector, "export_design", {"design_id": "../x", "format": "png"}),
            (
                "supabase",
                SupabaseConnector,
                "create_project",
                {"name": "n", "organization": "../x", "db_pass": "p"},
            ),
        ]
        for provider, cls, action, params in cases:
            with self.subTest(provider=provider, action=action):
                self.tokens.set(provider, f"{provider}-token")
                connector = cls(self.transport, self.tokens)
                with self.assertRaises(ConnectorError):
                    self.gate.execute(connector, action, params, approved=True)
        self.assertEqual(self.transport.calls, [])

    def test_a_gmail_query_cannot_add_parameters(self):
        self.tokens.set("gmail", "gm-token")
        gmail = GmailConnector(self.transport, self.tokens)
        self.gate.execute(gmail, "list_messages", {"q": "is:unread&maxResults=1"})
        url = self.transport.calls[0]["url"]
        self.assertIn("q=is%3Aunread%26maxResults%3D1", url)


if __name__ == "__main__":
    unittest.main()
