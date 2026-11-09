"""Hosted calls retry passing failures with backoff (model/remote_client.py).

Rate limits, 5xx / overloaded, dropped connections and in-stream "overloaded"
errors are retried before the user sees anything; key, credit and model
errors are not; a reply that already streamed text is never retried. The
waits are stubbed, so these tests take no real time and need no network.
"""
from __future__ import annotations

import pytest

from model import remote_client
from model.remote_client import (OpenAICompatClient, RemoteError, parse_retry_after, retry_attempts,
                                 retry_delay)
from tests.remote_fakes import FakeProvider


@pytest.fixture()
def fake():
    server = FakeProvider().start()
    yield server
    server.stop()


def make_client(fake: FakeProvider):
    client = OpenAICompatClient(fake.key, "fake/model", base_url=fake.base_url, label="Fake")
    waits: list[float] = []
    notices: list[dict] = []
    client._sleep = lambda seconds: waits.append(seconds) or False
    client._on_retry = notices.append
    return client, waits, notices


# ------------------------------------------------------------------ policy
def test_backoff_doubles_from_two_seconds_and_caps_at_thirty():
    assert [retry_delay(n, jitter=False) for n in range(1, 7)] == [2.0, 4.0, 8.0, 16.0, 30.0, 30.0]


def test_jitter_stays_within_twenty_percent():
    for _ in range(50):
        assert 3.2 <= retry_delay(2) <= 4.8


def test_retry_after_is_honoured_and_a_long_one_is_not_waited_for():
    assert retry_delay(1, 7.0) == 7.0
    assert retry_delay(1, remote_client.RETRY_AFTER_MAX_SECONDS + 1) is None


def test_retry_after_header_forms():
    assert parse_retry_after({"retry-after": "3"}) == 3.0
    assert parse_retry_after({"retry-after-ms": "1500", "retry-after": "9"}) == 1.5
    assert parse_retry_after({"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0  # in the past
    assert parse_retry_after({"retry-after": "soon"}) is None
    assert parse_retry_after({}) is None
    assert parse_retry_after(None) is None


def test_attempts_env(monkeypatch):
    monkeypatch.delenv("IMURUN_REMOTE_RETRIES", raising=False)
    assert retry_attempts() == remote_client.RETRY_ATTEMPTS
    monkeypatch.setenv("IMURUN_REMOTE_RETRIES", "1")
    assert retry_attempts() == 1
    monkeypatch.setenv("IMURUN_REMOTE_RETRIES", "junk")
    assert retry_attempts() == remote_client.RETRY_ATTEMPTS


# ------------------------------------------------------------------ retried
def test_rate_limit_then_success_retries_with_the_providers_wait(fake):
    fake.chat_failures = [(429, {"Retry-After": "5"}), (503, {})]
    client, waits, notices = make_client(fake)
    assert client.generate([{"role": "user", "content": "hi"}]) == "Hello from the fake"
    assert len(fake.chat_requests()) == 3
    assert waits[0] == 5.0                       # Retry-After wins
    assert 3.2 <= waits[1] <= 4.8                # then our own backoff (attempt 2)
    assert [n["kind"] for n in notices] == ["rate_limit", "server"]
    assert notices[0]["attempt"] == 1 and notices[0]["attempts"] == remote_client.RETRY_ATTEMPTS - 1
    assert client.stats()["retries"] == 2


def test_in_stream_overloaded_error_is_retried(fake):
    fake.chat_failures = [("stream_error", "Overloaded")]
    client, waits, notices = make_client(fake)
    assert client.generate([{"role": "user", "content": "hi"}]) == "Hello from the fake"
    assert notices[0]["kind"] == "overloaded" and len(waits) == 1


def test_unreachable_provider_is_retried_then_reported(monkeypatch):
    monkeypatch.setenv("IMURUN_REMOTE_RETRIES", "3")   # a refused connect costs ~2 s on Windows
    client = OpenAICompatClient("sk-x", "m", base_url="http://127.0.0.1:9/v1", label="Gone")
    waits: list[float] = []
    client._sleep = lambda seconds: waits.append(seconds) or False
    with pytest.raises(RemoteError) as info:
        client.generate([{"role": "user", "content": "hi"}])
    assert info.value.kind == "unreachable"
    assert len(waits) == 2


def test_gives_up_after_the_attempt_budget(fake, monkeypatch):
    monkeypatch.setenv("IMURUN_REMOTE_RETRIES", "3")
    fake.chat_failures = [(500, {})] * 5
    client, waits, _ = make_client(fake)
    with pytest.raises(RemoteError) as info:
        client.generate([{"role": "user", "content": "hi"}])
    assert info.value.kind == "server"
    assert len(fake.chat_requests()) == 3 and len(waits) == 2


# ------------------------------------------------------------------ not retried
@pytest.mark.parametrize("status,kind", [(401, "key"), (402, "credits"), (404, "model_gone"), (400, "other")])
def test_errors_waiting_cannot_fix_are_not_retried(fake, status, kind):
    fake.chat_failures = [(status, {})]
    client, waits, notices = make_client(fake)
    with pytest.raises(RemoteError) as info:
        client.generate([{"role": "user", "content": "hi"}])
    assert info.value.kind == kind
    assert len(fake.chat_requests()) == 1 and waits == [] and notices == []


def test_a_retry_after_beyond_the_limit_is_reported_at_once(fake):
    fake.chat_failures = [(429, {"Retry-After": "3600"})]
    client, waits, _ = make_client(fake)
    with pytest.raises(RemoteError) as info:
        client.generate([{"role": "user", "content": "hi"}])
    assert info.value.kind == "rate_limit" and waits == []


def test_a_reply_that_already_streamed_text_is_not_retried(fake):
    fake.chat_failures = [("partial_error", "overloaded")]
    client, waits, _ = make_client(fake)
    shown: list[str] = []
    with pytest.raises(RemoteError):
        client._stream_chunks([{"role": "user", "content": "hi"}], shown.append)
    assert shown == ["Half"] and waits == [] and len(fake.chat_requests()) == 1


def test_retries_off(fake, monkeypatch):
    monkeypatch.setenv("IMURUN_REMOTE_RETRIES", "1")
    fake.chat_failures = [(429, {})]
    client, waits, _ = make_client(fake)
    with pytest.raises(RemoteError):
        client.generate([{"role": "user", "content": "hi"}])
    assert waits == []


def test_cancel_during_the_wait_stops_quietly(fake):
    fake.chat_failures = [(503, {})]
    client, _, _ = make_client(fake)

    def cancelled_wait(_seconds: float) -> bool:
        client.cancel()
        return True

    client._sleep = cancelled_wait
    assert client.generate([{"role": "user", "content": "hi"}]) == ""
    assert len(fake.chat_requests()) == 1


def test_the_notice_never_carries_the_key(fake):
    fake.chat_error = f"slow down {fake.key}"
    fake.chat_failures = [(429, {"Retry-After": "0"})]
    client, _, notices = make_client(fake)
    client.generate([{"role": "user", "content": "hi"}])
    assert fake.key not in repr(notices)
