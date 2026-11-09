"""End-to-end server turn latency test.

Starts the real API server in-process on an ephemeral port with a temporary
data root, creates a session, runs one turn, and reports:

  * time to first streamed token (SSE ``token`` event)
  * total turn time
  * the answer text and engine stats

Run from the ``imurun`` directory:

    python tests/test_server_turn.py
    python tests/test_server_turn.py --prompt "What is 2+2?" --timeout 60
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.config import load_config  # noqa: E402
from services.server.app import build_server  # noqa: E402


def _request(port: int, token: str, method: str, path: str, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8", "replace")
    conn.close()
    try:
        return resp.status, json.loads(data)
    except json.JSONDecodeError:
        return resp.status, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt", default="Say hello in one short sentence.")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--mode", default="build")
    ap.add_argument("--warmup", action="store_true",
                    help="run a throwaway turn first so the model is resident")
    args = ap.parse_args()

    cfg = load_config()
    tmp = Path(tempfile.mkdtemp(prefix="imurun-test-"))
    t_build = time.time()
    server = build_server(root=tmp, port=0, config=cfg)
    server.start()
    build_s = time.time() - t_build
    port, token = server.port, server.token
    print(f"server     : http://127.0.0.1:{port}  (build {build_s:.2f}s)")

    status, body = _request(port, token, "GET", "/api/status")
    if status != 200:
        print(f"FAIL: /api/status -> {status} {body}")
        server.stop()
        return 2
    eng = body.get("engine", {})
    print(f"engine     : {eng.get('engine')}  model={body.get('selected_model')}")

    status, sess = _request(port, token, "POST", "/api/sessions",
                            {"first_command": "latency test"})
    if status != 200:
        print(f"FAIL: create session -> {status} {sess}")
        server.stop()
        return 2
    sid = sess["id"]

    if args.warmup:
        st, turn = _request(port, token, "POST", "/api/turns",
                            {"session_id": sid, "text": "hi", "mode": args.mode})
        if st == 200:
            wt = turn["turn_id"]
            wd = time.time() + args.timeout
            while time.time() < wd:
                st, view = _request(port, token, "GET", f"/api/turns/{wt}")
                if st == 200 and view.get("state") in ("done", "error", "cancelled"):
                    break
                time.sleep(0.05)
            print("warmup     : done")

    # Subscribe to SSE before starting the turn so no token is missed.
    first_token = {"at": None, "turn": None}
    stop = threading.Event()

    def _sse():
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=args.timeout + 10)
            conn.request("GET", "/api/events", headers={"Authorization": f"Bearer {token}"})
            resp = conn.getresponse()
            event_type = None
            while not stop.is_set():
                line = resp.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    event_type = line[6:].strip()
                elif line.startswith("data:") and event_type == "token":
                    try:
                        payload = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    if first_token["at"] is None and payload.get("token"):
                        first_token["at"] = time.time()
                        first_token["turn"] = payload.get("turn_id")
        except Exception:
            pass

    t = threading.Thread(target=_sse, daemon=True)
    t.start()
    time.sleep(0.3)

    t0 = time.time()
    status, turn = _request(port, token, "POST", "/api/turns",
                            {"session_id": sid, "text": args.prompt, "mode": args.mode})
    if status != 200:
        print(f"FAIL: start turn -> {status} {turn}")
        stop.set()
        server.stop()
        return 2
    tid = turn["turn_id"]

    state = None
    answer = ""
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        st, view = _request(port, token, "GET", f"/api/turns/{tid}")
        if st == 200:
            state = view.get("state")
            answer = view.get("answer") or ""
            if state in ("done", "error", "cancelled"):
                break
        time.sleep(0.05)
    total = time.time() - t0

    ttft = (first_token["at"] - t0) if first_token["at"] else None
    print(f"turn state : {state}")
    print(f"first token: {ttft:.2f}s" if ttft is not None else "first token: (none seen)")
    print(f"total      : {total:.2f}s")
    try:
        stats = server._model.stats()
        print(f"engine stats: {stats}")
    except Exception as exc:
        print(f"engine stats: (unavailable: {exc})")
    print("\n--- answer ---")
    print(answer.strip()[:1500])
    print("--------------")

    stop.set()
    server.stop()
    if state != "done":
        print(f"\nFAIL: turn ended in state {state}")
        return 1
    if ttft is not None and ttft > 2.0:
        print(f"\nWARN: first token took {ttft:.2f}s (> 2.0s target)")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
