"""Google OAuth: authorize URL + PKCE, code exchange, scopes, refresh, token store."""
from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlsplit

import pytest

from fakes_google import FakeGoogle
from services.connectors.gmail_setup import GmailSetup, scopes_for
from services.connectors.oauth import OAuthError, RefreshRejected, refresh_access_token
from services.connectors.providers.gmail import GmailConnector
from services.connectors.tokens import MemoryTokenStore
from services.connectors.wiring import FileTokenStore, dpapi_available
from services.server.settings import SettingsStore

READ = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SEND = "https://www.googleapis.com/auth/gmail.send"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    for key in ("CLIENT_ID", "CLIENT_SECRET", "SCOPES", "AUTHORIZE_URL", "TOKEN_URL", "REDIRECT_URI"):
        monkeypatch.delenv(f"IMURUN_OAUTH_GMAIL_{key}", raising=False)
    settings = SettingsStore(tmp_path / "settings")
    tokens = MemoryTokenStore()
    google = FakeGoogle()
    gmail = GmailSetup(settings=settings, tokens=tokens, form_transport=google,
                       redirect_uri="http://127.0.0.1:8732/oauth/callback")
    return gmail, settings, tokens, google


def test_scopes_are_the_minimum_for_enabled_tools():
    assert scopes_for(["gmail_search", "gmail_read"]) == (READ,)
    assert scopes_for(["gmail_draft"]) == (READ, COMPOSE)
    assert scopes_for(["gmail_send", "gmail_trash", "gmail_modify"]) == (READ, SEND, MODIFY)
    assert scopes_for(["gmail_search", "gmail_read", "gmail_labels", "gmail_draft", "gmail_send",
                       "gmail_modify", "gmail_trash"]) == (READ, COMPOSE, SEND, MODIFY)


def test_not_configured_has_no_flow(setup):
    gmail, *_ = setup
    assert not gmail.configured() and gmail.flow() is None


def test_authorize_url_and_pkce(setup):
    gmail, settings, tokens, google = setup
    gmail.update({"clientId": "123-abc.apps.googleusercontent.com", "clientSecret": "GOCSPX-shh",
                  "tools": ["gmail_search", "gmail_read", "gmail_draft"]})
    # The secret went to the token store, never to the settings file.
    assert "clientSecret" not in settings.read("connectors")["gmail"]
    assert "GOCSPX" not in (settings.root / "connectors.json").read_text()
    assert tokens.get("gmail:client_secret") == "GOCSPX-shh"
    assert gmail.view()["clientSecretSet"] is True and "GOCSPX" not in str(gmail.view())

    flow = gmail.flow()
    begun = flow.begin()
    url = urlsplit(begun["url"])
    assert url.scheme == "https" and url.netloc == "accounts.google.com"
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert query["client_id"] == "123-abc.apps.googleusercontent.com"
    assert query["redirect_uri"] == "http://127.0.0.1:8732/oauth/callback"
    assert query["scope"].split() == [READ, COMPOSE]
    assert query["code_challenge_method"] == "S256" and query["response_type"] == "code"
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["state"] == begun["state"]
    assert "client_secret" not in query and "GOCSPX" not in begun["url"]

    flow.complete(begun["state"], "auth-code")
    url_used, form = google.forms[-1]
    assert url_used == "https://oauth2.googleapis.com/token"
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code"
    assert form["client_secret"] == "GOCSPX-shh"
    # PKCE: the verifier sent at exchange time hashes to the challenge in the URL.
    digest = hashlib.sha256(form["code_verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == query["code_challenge"]
    assert tokens.get("gmail") == "access-1" and tokens.get("gmail:refresh") == "refresh-1"
    assert tokens.get("gmail:scope").split() == [READ, COMPOSE]
    with pytest.raises(OAuthError):     # the state is single-use
        flow.complete(begun["state"], "auth-code")


def test_refresh_stores_new_token_and_rejection_is_typed(setup):
    gmail, _, tokens, google = setup
    gmail.update({"clientId": "id.apps.googleusercontent.com"})
    with pytest.raises(OAuthError, match="no refresh token"):
        gmail.refresh()
    tokens.set("gmail", "access-1")
    tokens.set("gmail:refresh", "refresh-1")
    clock = lambda: 1000.0  # noqa: E731
    assert refresh_access_token(gmail.provider(), google, tokens, clock=clock) == "access-2"
    assert tokens.get("gmail") == "access-2" and tokens.get("gmail:expires_at") == str(1000.0 + 3599)
    assert tokens.get("gmail:refresh") == "refresh-1"      # not rotated by Google
    assert google.forms[-1][1]["grant_type"] == "refresh_token"
    google.refresh_ok = False
    with pytest.raises(RefreshRejected):
        gmail.refresh()


def test_connector_refreshes_before_expiry_and_on_401(setup):
    gmail, _, tokens, google = setup
    gmail.update({"clientId": "id.apps.googleusercontent.com"})
    now = [10_000.0]
    connector = GmailConnector(google, tokens, clock=lambda: now[0])
    connector.refresher = gmail.refresh
    tokens.set("gmail", "access-1")
    tokens.set("gmail:refresh", "refresh-1")
    tokens.set("gmail:expires_at", str(now[0] + 30))       # inside the 60 s margin
    assert connector.execute("profile", {})["emailAddress"] == "me@example.com"
    assert tokens.get("gmail") == "access-2"
    # Google revokes the access token early: 401 -> refresh once -> retry.
    google.valid_token = "access-3"
    google.refresh_ok = True

    def rotate():
        tokens.set("gmail", "access-3")
        return "access-3"

    connector.refresher = rotate
    tokens.set("gmail:expires_at", str(now[0] + 3600))
    assert connector.execute("list_labels", {})["labels"]
    # A refused refresh token forgets the account, so tools report "not connected".
    google.valid_token = "something-else"
    connector.refresher = gmail.refresh
    google.refresh_ok = False
    with pytest.raises(Exception, match="refused the refresh token"):
        connector.execute("list_labels", {})
    assert not connector.connected() and tokens.get("gmail:refresh") is None


def test_disconnect_revokes_and_forgets(setup):
    _, _, tokens, google = setup
    connector = GmailConnector(google, tokens)
    for key, value in (("gmail", "a"), ("gmail:refresh", "r"), ("gmail:scope", READ), ("gmail:client_secret", "s")):
        tokens.set(key, value)
    assert connector.disconnect() is True
    assert google.forms[-1] == ("https://oauth2.googleapis.com/revoke", {"token": "r"})
    assert tokens.get("gmail") is None and tokens.get("gmail:refresh") is None
    assert tokens.get("gmail:client_secret") == "s"        # the app config stays


def test_file_token_store_protects_values(tmp_path):
    store = FileTokenStore(tmp_path / "secrets" / "connectors.json")
    store.set("gmail", "ya29.secret-token")
    raw = (tmp_path / "secrets" / "connectors.json").read_text()
    assert store.get("gmail") == "ya29.secret-token"
    if dpapi_available():
        assert "ya29" not in raw and "dpapi:" in raw
    # Plain values written by older versions are still read.
    plain = FileTokenStore(tmp_path / "old.json", protect=False)
    plain.set("github", "ghp_x")
    assert FileTokenStore(tmp_path / "old.json").get("github") == "ghp_x"
    store.delete("gmail")
    assert store.get("gmail") is None
