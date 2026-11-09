"""The agentic HTTP routes on a real in-process ApiServer with a fake model."""
from __future__ import annotations

import http.client
import json
import time

import pytest

from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills.store import SkillsStore
from services.tools import ToolRunner, default_registry


class FakeModel:
    available = True

    def __init__(self):
        self.replies: list[str] = []
        self.seen: list[list[dict]] = []

    def generate(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0) if self.replies else "ok"

    def context_limit(self):
        return 4096


@pytest.fixture()
def api(tmp_path):
    model = FakeModel()
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions",
        data_root=tmp_path,
        model=model,
        # ApiServer's default SkillsStore is ~/.imurun/skills regardless of
        # data_root: always inject one so tests never touch the real home.
        skills=SkillsStore(tmp_path / "skills"),
        tools=ToolRunner(default_registry()),
        static_root=None,
    ).start()
    server._settings.write("permissions", {"workspaceRoot": str(tmp_path / "ws")})
    yield server, model, tmp_path
    server.stop()


def req(server, method, path, payload=None, token=True):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    headers = {"Authorization": f"Bearer {server.token}"} if token else {}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, (json.loads(data) if data else {})


def test_health_and_auth(api):
    server, _, _ = api
    assert req(server, "GET", "/health", token=False) == (200, {"ok": True})
    assert req(server, "GET", "/api/tools", token=False)[0] == 401


def test_tools_listing_has_schemas_and_permissions(api):
    server, _, _ = api
    status, body = req(server, "GET", "/api/tools")
    assert status == 200
    tools = {t["name"]: t for t in body["tools"]}
    for name in ("web_search", "web_fetch", "file_read", "file_write", "file_list", "make_dir",
                 "file_move", "file_copy", "file_delete", "shell", "run_agent", "time_now"):
        assert name in tools, name
    assert tools["file_read"]["parameters"]["required"] == ["path"]
    assert tools["file_read"]["permission"] == "allow"
    assert tools["shell"]["permission"] == "ask"
    assert body["permissions"]["mode"] == "allow-reads"


def test_permissions_round_trip(api, tmp_path):
    server, _, _ = api
    status, view = req(server, "GET", "/api/permissions")
    assert status == 200 and view["mode"] == "allow-reads" and view["workspace"].endswith("ws")
    assert req(server, "POST", "/api/permissions", {"mode": "bogus"})[0] == 400
    status, view = req(server, "POST", "/api/permissions", {"mode": "ask"})
    assert status == 200 and view["mode"] == "ask"
    # The same values are visible through the existing settings route.
    assert req(server, "GET", "/api/settings/permissions")[1]["values"]["mode"] == "ask"


def test_manual_run_needs_approval_for_writes(api):
    server, _, root = api
    payload = {"name": "file_write", "args": {"path": "hello.txt", "content": "hi"}}
    status, body = req(server, "POST", "/api/tools/run", payload)
    assert status == 403 and body["decision"] == "ask"
    status, body = req(server, "POST", "/api/tools/run", {**payload, "approved": True})
    assert status == 200 and body["result"]["result"]["ok"]
    assert (root / "ws" / "hello.txt").read_text() == "hi"
    status, body = req(server, "POST", "/api/tools/run", {"name": "file_read", "args": {"path": "hello.txt"}})
    assert status == 200 and "hi" in body["result"]["result"]["content"]
    status, body = req(server, "POST", "/api/tools/run", {"name": "file_read", "args": {"path": "../x"}})
    assert status == 403 and body["decision"] == "deny"
    assert req(server, "POST", "/api/tools/run", {"name": ""})[0] == 400


def test_approvals_endpoint(api):
    server, _, _ = api
    assert req(server, "GET", "/api/tools/approvals") == (200, {"approvals": []})
    assert req(server, "POST", "/api/tools/approvals/unknown", {"approved": True})[0] == 404


def test_agent_settings(api):
    server, _, _ = api
    assert req(server, "GET", "/api/agent")[1]["maxSteps"] == 6
    assert req(server, "POST", "/api/agent", {"maxSteps": 99})[0] == 400
    assert req(server, "POST", "/api/agent", {"maxSteps": 3})[1]["maxSteps"] == 3


def test_skills_get_and_delete(api):
    server, _, _ = api
    status, skill = req(server, "POST", "/api/skills",
                        {"name": "Release notes", "description": "Write notes", "body": "Use bullets."})
    assert status == 200
    status, one = req(server, "GET", f"/api/skills/{skill['id']}")
    assert status == 200 and one["invoke"] == "/release-notes"
    assert req(server, "DELETE", f"/api/skills/{skill['id']}")[0] == 200
    assert req(server, "GET", f"/api/skills/{skill['id']}")[0] == 404


def test_turn_with_model_tool_call_and_skill(api):
    server, model, root = api
    (root / "ws").mkdir(exist_ok=True)
    (root / "ws" / "todo.md").write_text("- buy milk")
    req(server, "POST", "/api/skills", {"name": "terse", "description": "short", "body": "Answer in five words."})
    model.replies = ['```tool\n{"name": "file_read", "args": {"path": "todo.md"}}\n```',
                     "You need to buy milk."]
    status, session = req(server, "POST", "/api/sessions", {"first_command": "todo"})
    assert status == 200
    status, turn = req(server, "POST", "/api/turns", {"session_id": session["id"], "text": "/terse what is on my todo list"})
    assert status == 200
    view = {}
    for _ in range(100):
        status, view = req(server, "GET", f"/api/turns/{turn['turn_id']}")
        if view.get("state") != "running":
            break
        time.sleep(0.05)
    assert view["state"] == "done", view
    assert view["answer"] == "You need to buy milk." and view["tool_calls"] == 1
    assert view["skills"] == ["terse"]
    assert "Answer in five words." in model.seen[0][0]["content"]
    assert model.seen[0][-1]["content"] == "what is on my todo list"
    model.available = False
    assert req(server, "POST", "/api/turns", {"session_id": session["id"], "text": "hi"})[0] == 503
    _, messages = req(server, "GET", f"/api/sessions/{session['id']}/messages")
    roles = [(m["role"], m["content"]) for m in messages["messages"]]
    assert roles == [("user", "/terse what is on my todo list"), ("assistant", "You need to buy milk.")]
