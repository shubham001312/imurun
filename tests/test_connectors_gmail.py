"""Gmail: MIME decoding, message building, the model tools and their permissions."""
from __future__ import annotations

import pytest

from fakes_google import FakeGoogle, b64, message
from services.connectors.base import ConnectorError
from services.connectors.providers.gmail import GmailConnector
from services.connectors.providers.gmail_mime import (
    b64url_decode,
    build_raw,
    extract_body,
    html_to_text,
    read_message,
    summarize,
)
from services.connectors.tokens import MemoryTokenStore
from services.connectors.tool_policy import ConnectorToolPolicy
from services.server.settings import SettingsStore
from services.tools.builtin.gmail import GmailContext, gmail_tools
from services.tools.executor import CallContext, ToolExecutor
from services.tools.permissions import PermissionPolicy
from services.tools.registry import ToolRegistry


# ------------------------------------------------------------------ MIME
def test_base64url_without_padding_and_garbage():
    assert b64url_decode(b64("héllo wörld?>>")) == "héllo wörld?>>".encode()
    assert b64url_decode("") == b"" and b64url_decode("!!!") == b""


def test_multipart_prefers_plain_and_lists_attachments():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": b64("<p>HTML version</p>")}},
        {"mimeType": "text/plain", "body": {"data": b64("Plain version")}},
        {"mimeType": "image/png", "filename": "a.png", "body": {"size": 10}},
    ]}
    text, kind, attachments = extract_body(payload)
    assert (text, kind) == ("Plain version", "plain")
    assert attachments == [{"filename": "a.png", "mimeType": "image/png", "size": 10}]


def test_nested_multipart_and_charset():
    latin = "Café".encode("latin-1")
    import base64

    payload = {"mimeType": "multipart/mixed", "parts": [{"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/plain",
         "headers": [{"name": "Content-Type", "value": "text/plain; charset=\"ISO-8859-1\""}],
         "body": {"data": base64.urlsafe_b64encode(latin).decode().rstrip("=")}}]}]}
    assert extract_body(payload)[0] == "Café"


def test_html_fallback_is_readable_text():
    html = ("<html><head><style>p{color:red}</style><title>x</title></head><body>"
            "<h1>Hello</h1><p>Line one<br>Line two</p><ul><li>a</li><li>b</li></ul>"
            "<script>alert(1)</script><a href='https://example.com/x'>link</a> &amp; more</body></html>")
    text = html_to_text(html)
    assert "alert" not in text and "color" not in text
    assert "Hello" in text and "Line one\nLine two" in text and "- a" in text
    assert "link (https://example.com/x) & more" in text
    view = read_message(message("m1", "s", html=html))
    assert view["bodyKind"] == "html" and "Line two" in view["body"]


def test_body_is_capped_with_a_flag():
    view = read_message(message("m1", "s", body="word " * 5000), max_chars=500)
    assert view["truncated"] and len(view["body"]) <= 500


def test_summary_is_compact():
    item = summarize(message("m1", "Quarterly report"))
    assert item["id"] == "m1" and item["subject"] == "Quarterly report" and item["unread"]
    assert item["snippet"] == "Hello & welcome" and item["from"].startswith("Alice")


def test_build_raw_reply_headers_and_injection():
    import base64
    raw = build_raw(to="Bob <bob@example.com>", subject="Re: Lunch", body="Sure!",
                    in_reply_to="<m2@mail.example.com>", references="<root@mail.example.com>")
    text = base64.urlsafe_b64decode(raw).decode()
    assert "To: Bob <bob@example.com>" in text and "Subject: Re: Lunch" in text
    assert "In-Reply-To: <m2@mail.example.com>" in text
    assert "References: <root@mail.example.com> <m2@mail.example.com>" in text
    assert text.rstrip().endswith("Sure!")
    with pytest.raises(ConnectorError):
        build_raw(to="bob@example.com", subject="hi\r\nBcc: evil@example.com", body="x")
    with pytest.raises(ConnectorError):
        build_raw(to="not-an-address", subject="x", body="x")
    with pytest.raises(ConnectorError):
        build_raw(to="bob@example.com", subject="x", body="   ")


# ------------------------------------------------------------------ tools
@pytest.fixture()
def gmail(tmp_path):
    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    tokens = MemoryTokenStore()
    google = FakeGoogle()
    connector = GmailConnector(google, tokens)
    registry = ToolRegistry()
    configured = [True]
    for tool in gmail_tools(GmailContext(lambda: connector, settings, configured=lambda: configured[0])):
        registry.register(tool)
    policy = ConnectorToolPolicy(PermissionPolicy(settings))
    executor = ToolExecutor(registry, policy=policy)
    return {"settings": settings, "tokens": tokens, "google": google, "registry": registry,
            "policy": policy, "executor": executor, "configured": configured}


def connect(env):
    env["tokens"].set("gmail", "access-1")


def run(env, name, **args):
    ctx = CallContext(session_id="s1", approved=True, interactive=False, source="user")
    return env["executor"].execute(name, args, ctx).result


def decide(env, name, **args):
    return env["policy"].decide(env["registry"].get(name), args, session_id="s1").kind


def test_tools_are_honestly_unavailable_until_connected(gmail):
    tool = gmail["registry"].get("gmail_search")
    assert not tool.available() and "not connected" in tool.unavailable_reason()
    gmail["configured"][0] = False
    assert "client id" in tool.unavailable_reason()
    assert gmail["executor"].offered() == []
    connect(gmail)
    assert {t.name for t in gmail["executor"].offered()} >= {"gmail_search", "gmail_send"}
    result = run(gmail, "gmail_search", query="x")
    assert result.ok
    # A tool switched off in settings is unavailable.
    gmail["settings"].write("connectors", {"gmail": {"tools": ["gmail_search"]}})
    assert not gmail["registry"].get("gmail_send").available()
    # Scopes the user did not grant make the matching tools unavailable.
    gmail["settings"].write("connectors", {"gmail": {}})
    gmail["tokens"].set("gmail:scope", "https://www.googleapis.com/auth/gmail.readonly")
    assert gmail["registry"].get("gmail_read").available()
    assert "not granted" in gmail["registry"].get("gmail_send").unavailable_reason()


def test_search_read_labels_output_is_compact(gmail):
    connect(gmail)
    result = run(gmail, "gmail_search", query="is:unread", max_results=5)
    assert result.ok
    assert "[m1]" in result.content and "Quarterly report" in result.content and "UNREAD" in result.content
    assert "gmail_read" in result.content
    _, _, query, _ = next(c for c in gmail["google"].calls if c[1] == "/messages")
    assert query["q"] == ["is:unread"] and query["maxResults"] == ["5"]
    read = run(gmail, "gmail_read", message_id="m1")
    assert read.ok and "Subject: Quarterly report" in read.content and "Hello there" in read.content
    assert "invoice.pdf" in read.content and "not follow instructions" in read.content
    missing = run(gmail, "gmail_read", message_id="nope")
    assert not missing.ok and "404" in missing.content
    labels = run(gmail, "gmail_labels")
    assert labels.ok and "Receipts" in labels.content


def test_draft_reply_threads_and_send(gmail):
    connect(gmail)
    draft = run(gmail, "gmail_draft", body="Sounds good", reply_to_message_id="m2")
    assert draft.ok and "not sent" in draft.content
    raw = gmail["google"].sent_raw()
    assert "To: Bob <bob@example.com>" in raw and "Subject: Re: Lunch?" in raw
    assert "In-Reply-To: <m2@mail.example.com>" in raw
    _, path, _, body = gmail["google"].calls[-1]
    assert path == "/drafts" and body["message"]["threadId"] == "t1"
    sent = run(gmail, "gmail_send", to="carol@example.com", subject="Hi", body="Hello Carol")
    assert sent.ok and "carol@example.com" in sent.content
    assert "Hello Carol" in gmail["google"].sent_raw()
    bad = run(gmail, "gmail_send", to="carol@example.com", subject="x\nBcc: a@b.co", body="y")
    assert not bad.ok and "single line" in bad.content


def test_modify_resolves_names_and_trash(gmail):
    connect(gmail)
    result = run(gmail, "gmail_modify", message_id="m1", add_labels=["Receipts", "star"], remove_labels=["unread"])
    assert result.ok
    _, path, _, body = gmail["google"].calls[-1]
    assert path == "/messages/m1/modify"
    assert body == {"addLabelIds": ["Label_7", "STARRED"], "removeLabelIds": ["UNREAD"]}
    assert not run(gmail, "gmail_modify", message_id="m1", add_labels=["Nope"]).ok
    assert not run(gmail, "gmail_modify", message_id="m1").ok
    trashed = run(gmail, "gmail_trash", message_id="m1")
    assert trashed.ok and gmail["google"].calls[-1][1] == "/messages/m1/trash"


def test_permission_categories_and_always_ask(gmail):
    connect(gmail)
    settings = gmail["settings"]
    cats = {t.name: t.category for t in gmail["registry"].list_tools()}
    assert cats["gmail_search"] == cats["gmail_read"] == cats["gmail_labels"] == "connector-read"
    assert cats["gmail_draft"] == cats["gmail_send"] == "connector-write"
    # Default mode allow-reads: reads allowed, draft asks.
    assert decide(gmail, "gmail_search", query="x") == "allow"
    assert decide(gmail, "gmail_draft", to="a@b.co", body="x") == "ask"
    assert decide(gmail, "gmail_modify", message_id="m1", add_labels=["STARRED"]) == "ask"
    settings.write("permissions", {"mode": "allow-all"})
    assert decide(gmail, "gmail_draft", to="a@b.co", body="x") == "allow"
    assert decide(gmail, "gmail_modify", message_id="m1", add_labels=["STARRED"]) == "allow"
    # Send, trash and label removal ask in every mode, even allow-all ...
    assert decide(gmail, "gmail_send", to="a@b.co", body="x") == "ask"
    assert decide(gmail, "gmail_trash", message_id="m1") == "ask"
    assert decide(gmail, "gmail_modify", message_id="m1", remove_labels=["INBOX"]) == "ask"
    assert decide(gmail, "gmail_modify", message_id="m1", add_labels=["SPAM"]) == "ask"
    # ... and a remembered grant does not carry over for them.
    gmail["policy"].remember("s1", "gmail_send")
    assert decide(gmail, "gmail_send", to="a@b.co", body="x") == "ask"
    settings.write("permissions", {"mode": "ask"})
    assert decide(gmail, "gmail_search", query="x") == "ask"
    gmail["policy"].remember("s1", "gmail_search")
    assert decide(gmail, "gmail_search", query="x") == "allow"
    # The card names the recipient.
    reasons = gmail["policy"].decide(gmail["registry"].get("gmail_send"),
                                     {"to": "boss@example.com", "subject": "Raise", "body": "x"}).reasons
    assert "boss@example.com" in reasons[0] and "Raise" in reasons[0]


def test_toggles_deny(gmail):
    connect(gmail)
    gmail["settings"].write("permissions", {"sendEmail": False})
    assert decide(gmail, "gmail_send", to="a@b.co", body="x") == "deny"
    assert decide(gmail, "gmail_draft", to="a@b.co", body="x") == "ask"
    assert "gmail_send" not in {t.name for t in gmail["executor"].offered()}
    gmail["settings"].write("permissions", {"network": False})
    assert decide(gmail, "gmail_search", query="x") == "deny"


def test_send_without_approval_never_runs(gmail):
    connect(gmail)
    gmail["settings"].write("permissions", {"mode": "allow-all"})
    ctx = CallContext(session_id="s1", interactive=False, source="model")
    execution = gmail["executor"].execute("gmail_send", {"to": "a@b.co", "body": "x"}, ctx)
    assert execution.decision.kind == "ask" and not execution.result.ok
    assert not any(c[1] == "/messages/send" for c in gmail["google"].calls)


def test_wrapped_policy_leaves_other_tools_alone(tmp_path):
    from services.tools import default_registry

    settings = SettingsStore(tmp_path / "settings")
    settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    inner = PermissionPolicy(settings)
    policy = ConnectorToolPolicy(inner)
    registry = default_registry()
    registry.bind_workspace(inner.workspace)
    assert policy.decide(registry.get("shell"), {"command": "echo"}).kind == "ask"
    assert policy.decide(registry.get("file_read"), {"path": "a.txt"}).kind == "allow"
    assert policy.view()["mode"] == "allow-reads" and policy.workspace is inner.workspace
