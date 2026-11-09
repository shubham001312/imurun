# Research engine — requests to other owners

Everything below works without these changes (the pre-fetch falls back to the
heuristic plan). Each request turns a fallback into the real thing.

## R1 — `services/response/loop.py`: model-planned pre-fetch (owner: loop / agentic)  — OPEN

Today `_run_prefetch` calls `self._tools.intents(prompt)` and
`self._tools.observe_full(prompt)` with the prompt only, so the runner has no
model to plan with and uses the regex heuristic. Pass the conversation and the
**raw** model (not the publishing one — the routing generation must not stream
into the answer bubble):

```python
def _run_prefetch(self, prompt: str, state: _TurnState) -> str:
    if self._tools is None:
        return ""
    try:
        extra = {}
        if getattr(self._tools, "supports_research", False):
            extra = {
                "history": [m for m in self._prior_messages[-HISTORY_MESSAGES:]
                            if m.get("role") in ("user", "assistant")] if self._use_history else [],
                "model": self._raw_model,
                "publish": self._publish if self._events is not None else None,
                "turn_id": self._turn_id,
            }
        names = list(self._tools.intents(prompt, **extra)) if hasattr(self._tools, "intents") else []
        if not names:
            return ""
        searching = "web_search" in names
        plan = self._tools.plan(prompt, extra.get("history"), extra.get("model")) if extra and searching else None
        if plan is not None:
            state.needs_web = bool(plan.needs_web)          # new _TurnState field, default False
        self._publish("tools.start", {"tools": names})
        if searching:
            self._publish("web_search.start", {"query": prompt,
                                               "queries": list(getattr(plan, "queries", []) or [])})
        if hasattr(self._tools, "observe_full"):
            text, results = self._tools.observe_full(prompt, **extra, stop=self.stopped) if extra \
                else self._tools.observe_full(prompt)
        ...  # unchanged from here
```

`intents()` and `observe_full()` share the plan through the planner's cache
(keyed on prompt + history), so this is **one** routing generation per
message; `plan()` is also a cache hit.

And in `_result`, so a question the *model* judged live is marked
`unverified` when no live result came back (today only the regex decides):

```python
elif is_realtime(state.question) or getattr(state, "needs_web", False):
    verification = "unverified"
```

Optional, same file: `LIVE_DATA_RULE` users may now cite `[n]`; no change
needed — the Context block itself says "Cite the passages you use as [n]".

## R2 — `window.py`: the search helper mode (owner: launcher / setup) — DONE

`window.py --search-helper <server_pid>` exists (`window.py::search_helper`,
to the spec in `BROWSER-SEARCH.md`) and was verified live on Windows 11 on
2026-11-09 (hidden, private mode, shut down after the check). Since that day
the server starts it on demand **by default** (`IMURUN_BROWSER_SEARCH=0`
switches it off) and stops it after 10 idle minutes. No `start.py` change was
needed. Please keep the literal `--search-helper` in `window.py` (the server
detects support by it) and the `hidden=True` / `private_mode=True` /
`_watch_parent` lines (a test checks them).

## R3 — `services/server/app.py`: event sink + planner model (owner: server) — OPEN

1. After the event broker exists:
   `from ..research import set_publisher; set_publisher(server.events.publish)`
   (or whatever the broker's publish callable is). Model-issued `web_search`
   calls then emit `research.*` and `web_search.start/done` with `turn_id`;
   without it those events are dropped (the tool result is unaffected).
2. Optional fallback when R1 is not done yet: `tools.planner_model = model`
   on the `ToolRunner` created in `build_server` — the runner then plans with
   the model even though the loop passes only the prompt. (Not needed once R1
   lands; harmless if both are done.)

## R4 — `services/server/doc/README.md`: document the new events (owner: server) — OPEN

Add to the Events table:

| Event | Payload |
|---|---|
| `research.plan` | `{turn_id, needs_web, reason, queries, freshness, entities, method: model\|heuristic\|prefilter\|given, elapsed}` |
| `research.search` | `{turn_id, source, query, ok, results, elapsed, error?, blocked?}` (also `{source, ok: false, skipped: true, reason}` in the result data for unavailable sources) |
| `research.fetch` | `{turn_id, url, status, ok, chars, error, lang?, published?}` |
| `research.done` | `{turn_id, ok, queries, sources: [{title, url, domain, provider, snippet}], passages, hits, errors, offline, blocked, elapsed}` |
| `web_search.start` / `web_search.done` | now also emitted for model-issued calls, with `turn_id` and `queries` |

## R6 — frontend: research lanes for the new sources (owner: frontend) — OPTIONAL

`research.js` maps providers to lanes (Wikipedia / Public search / News /
Direct); every new provider currently falls into "Public search", which is
right for `browser:duckduckgo`, `browser:bing`, `mwmbl`, `searxng`, but hides
what answered a code or science question. Suggested `LANE_OF` additions:

```js
ddg_instant: 'wikipedia',           // reference abstracts
stackexchange: 'code', github: 'code', hackernews: 'code',
arxiv: 'science', openalex: 'science',
endoflife: 'direct', pypi: 'direct', npm: 'direct'
```

with two lanes `{ id: 'code', label: 'Developer Q&A' }` and
`{ id: 'science', label: 'Papers' }`. `research.search` frames carry
`source` = the source name (`stackexchange`, `github_search`, `hackernews`,
`arxiv`, `openalex`, `endoflife`, `ddg_instant`, `mwmbl`, `browser`), and
`sources[].provider` the provider names above. No API change is needed.

The search-key UI was removed on 2026-11-09 (owner's rule: no keys for web
search). Please do not bring back a provider/key panel.

## R5 — frontend (owner: frontend) — OPTIONAL

Render a compact research trace from `research.*` (sources asked, pages read,
elapsed) in the thinking panel, and link `[n]` in answers to the matching
source chip (`sources[n-1]`, same order as the Context block).
