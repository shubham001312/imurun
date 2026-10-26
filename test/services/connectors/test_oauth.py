"""OAuth: PKCE, one-time state, loopback redirect, keychain storage."""
import base64
import hashlib
import unittest
from urllib.parse import parse_qs, urlsplit

from services.connectors.oauth import OAuthError, OAuthFlow, OAuthProvider
from services.connectors.tokens import MemoryTokenStore

PROVIDER = OAuthProvider(
    id="github",
    authorize_url="https://github.com/login/oauth/authorize",
    token_url="https://github.com/login/oauth/access_token",
    scopes=("repo", "read:user"),
    client_id="client-123",
)

REDIRECT = "http://127.0.0.1:8765/callback"


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeOAuthTransport:
    def __init__(self, response=None, error=None):
        self.response = response or {"access_token": "gho_secret_token_1234"}
        self.error = error
        self.calls = []

    def post_form(self, url, data):
        self.calls.append({"url": url, "data": dict(data)})
        if self.error is not None:
            raise self.error
        return dict(self.response)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.tokens = MemoryTokenStore()
        self.transport = FakeOAuthTransport()
        self.clock = FakeClock()
        self.marks = []
        self.flow = OAuthFlow(
            PROVIDER,
            self.transport,
            self.tokens,
            REDIRECT,
            mark=lambda rule, subject, why: self.marks.append((rule, subject, why)),
            clock=self.clock,
        )

    def test_begin_builds_a_pkce_url_without_the_secret(self):
        started = self.flow.begin()
        parsed = urlsplit(started["url"])
        query = parse_qs(parsed.query)
        self.assertEqual(query["client_id"], ["client-123"])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        self.assertEqual(query["scope"], ["repo read:user"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["state"], [started["state"]])
        self.assertNotIn("client_secret", query)

    def test_every_begin_has_a_fresh_state(self):
        self.assertNotEqual(self.flow.begin()["state"], self.flow.begin()["state"])

    def test_complete_exchanges_the_code_and_stores_the_token(self):
        started = self.flow.begin()
        token = self.flow.complete(started["state"], "code-1")
        self.assertEqual(token, "gho_secret_token_1234")
        self.assertEqual(self.tokens.get("github"), "gho_secret_token_1234")
        call = self.transport.calls[0]
        self.assertEqual(call["url"], PROVIDER.token_url)
        self.assertEqual(call["data"]["code"], "code-1")
        self.assertEqual(call["data"]["grant_type"], "authorization_code")
        self.assertEqual(self.marks[-1][2], "connected")

    def test_the_verifier_matches_the_challenge(self):
        started = self.flow.begin()
        challenge = parse_qs(urlsplit(started["url"]).query)["code_challenge"][0]
        self.flow.complete(started["state"], "code-1")
        verifier = self.transport.calls[0]["data"]["code_verifier"]
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        self.assertEqual(expected, challenge)

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(OAuthError):
            self.flow.complete("made-up", "code-1")
        self.assertEqual(self.transport.calls, [])
        self.assertIn("refused", self.marks[-1][2])

    def test_state_is_one_time(self):
        started = self.flow.begin()
        self.flow.complete(started["state"], "code-1")
        with self.assertRaises(OAuthError):
            self.flow.complete(started["state"], "code-2")

    def test_an_expired_state_is_refused(self):
        started = self.flow.begin()
        self.clock.now += 601
        with self.assertRaises(OAuthError):
            self.flow.complete(started["state"], "code-1")

    def test_a_provider_error_is_an_oauth_error_and_marks(self):
        flow = OAuthFlow(
            PROVIDER,
            FakeOAuthTransport(error=RuntimeError("boom")),
            self.tokens,
            REDIRECT,
            mark=lambda rule, subject, why: self.marks.append((rule, subject, why)),
        )
        started = flow.begin()
        with self.assertRaises(OAuthError):
            flow.complete(started["state"], "code-1")
        self.assertIsNone(self.tokens.get("github"))
        self.assertIn("refused", self.marks[-1][2])

    def test_a_missing_access_token_is_refused(self):
        flow = OAuthFlow(
            PROVIDER,
            FakeOAuthTransport(response={"error": "bad"}),
            self.tokens,
            REDIRECT,
        )
        started = flow.begin()
        with self.assertRaises(OAuthError):
            flow.complete(started["state"], "code-1")

    def test_the_client_secret_is_only_in_the_exchange(self):
        provider = OAuthProvider(
            id="gmail",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            scopes=("gmail.readonly",),
            client_id="client-456",
            client_secret="super-secret",
        )
        transport = FakeOAuthTransport()
        flow = OAuthFlow(provider, transport, self.tokens, REDIRECT)
        started = flow.begin()
        self.assertNotIn("super-secret", started["url"])
        flow.complete(started["state"], "code-1")
        self.assertEqual(transport.calls[0]["data"]["client_secret"], "super-secret")

    def test_a_non_loopback_redirect_is_refused(self):
        with self.assertRaises(ValueError):
            OAuthFlow(PROVIDER, self.transport, self.tokens, "https://evil.example/cb")

    def test_a_non_https_endpoint_is_refused(self):
        provider = OAuthProvider(
            id="x",
            authorize_url="http://example.com/auth",
            token_url="https://example.com/token",
            scopes=(),
            client_id="c",
        )
        with self.assertRaises(ValueError):
            OAuthFlow(provider, self.transport, self.tokens, REDIRECT)


if __name__ == "__main__":
    unittest.main()
