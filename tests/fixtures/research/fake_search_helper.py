"""A stand-in for `window.py --search-helper` speaking protocol v1 (tests only).

It never opens a browser: a "search" answers with canned results so the
server-side client (services/research/sources/browser_search.py) can be
tested end to end over real pipes.
"""
import json
import sys


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def main():
    protocol = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    send({"type": "ready", "protocol": protocol, "engines": ["duckduckgo", "bing"]})
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        op = request.get("op")
        if op == "shutdown":
            return
        if op == "ping":
            send({"id": request.get("id"), "type": "pong"})
            continue
        if op != "search":
            send({"id": request.get("id"), "ok": False, "error": f"unknown op {op}"})
            continue
        url = request.get("url", "")
        if not request.get("script"):
            send({"id": request.get("id"), "ok": False, "error": "no script"})
        elif "blocked" in url:
            send({"id": request.get("id"), "ok": True, "result": {"status": "blocked"}, "final_url": url})
        elif "bing.com" in url:
            send({"id": request.get("id"), "ok": True, "final_url": url, "elapsed_ms": 5, "result": {
                "status": "ok", "results": [{"title": "Bing hit", "url": "https://bing-result.example/", "snippet": "b"}]}})
        else:
            send({"id": request.get("id"), "ok": True, "final_url": url, "elapsed_ms": 5, "result": {
                "status": "ok", "results": [
                    {"title": "Northhaven port strike ends",
                     "url": "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example.com%2Fstrike&rut=x",
                     "snippet": "Dock workers accepted a 9% pay rise."},
                    {"title": "", "url": "https://nope.example/"},
                    {"title": "Not http", "url": "javascript:alert(1)"},
                ]}})


if __name__ == "__main__":
    main()
