# Agentic layer — bugs fixed

## 2026-11-02 — macOS: the user's temp dir was "a system location"

Symptom: 16 failures in GitHub CI on macOS only - every file/shell tool test
got `Permission denied ... /private/var/folders/.../T/pytest-.../ws/a.txt is a
system location`. Cause: macOS resolves the per-user temp dir to
`/private/var/folders/...`, and `/private/var` is on `SYSTEM_PREFIXES`
(`services/safety/decisions.py`). Fix: `TEMP_PREFIXES` (the process temp dir
plus `/tmp`, `/var/tmp`, `/var/folders` and their `/private` forms) is checked
first and is never a system location. Test: `test_temp_dirs_are_not_system_locations`.

Symptom → cause → fix → where (line numbers as of 2026-10-31).

1. **"Inappropriate answers about real-time data."**
   - Cause: tools ran only when a narrow regex matched; the model could never
     call one; the house style had no rule about live data; and the system
     prompt did not say what day it is.
   - Fix: model-driven multi-step tool loop, `LIVE_DATA_RULE`, today's date in
     the system prompt, a realtime nudge, and the same offline wording in the
     pre-fetch.
   - Where: `services/response/loop.py` (`run`, `_needs_live_nudge`),
     `services/response/style.py:24`, `services/tools/policy.py:186`.

2. **Web search silently returned "nothing" on this machine.**
   - Cause: DuckDuckGo's keyless HTML endpoint answers automated requests with a
     bot-check page. The regex found no results, the instant-answer fallback
     also returned nothing, and the tool reported `ok: true`, "returned
     nothing". The model then answered from memory.
   - Fix: the bot-check page now raises a failure that names the keyed
     providers, and the tool returns `ok: false`. The model is told the search
     failed.
   - Where: `services/tools/builtin/web_search.py:185` and `_search_duckduckgo`.

3. **Every turn duplicated the conversation history into the transcript.**
   - Cause: `TurnRunner._append_result` walked `result.messages`, which also
     holds the prior history given to the loop. It skipped only "the first
     user message" (which was a history message, not the new prompt) and
     re-appended the rest.
   - Fix: the loop returns `new_messages`, and only those are written.
   - Where: `services/server/turns.py:569`, `LoopResult.new_messages` in `loop.py`.

4. **`DELETE /api/skills/{id}` dropped the connection.**
   - Cause: the route called `SkillsStore.delete`, which did not exist
     (AttributeError inside the handler).
   - Fix: `delete()` was implemented. It removes only the files the store
     writes.
   - Where: `services/skills/store.py:86`.

5. **The model chosen in the UI was never remembered across restarts.**
   - Cause: `POST /api/models/select` called `save_last_model(server.data_root, ...)`.
     `ApiServer` has no `data_root`, and the bare `except` swallowed the
     AttributeError.
   - Fix: pass `server._home`.
   - Where: `services/server/api.py:1078`.

6. **`import services.response` failed with a circular import.**
   - Cause: `loop.py` imported `services.server.events` at runtime, which loads
     `services/server/__init__` → `api` → `turns` → the half-initialised `loop`.
     It only worked when `services.server` happened to be imported first.
   - Fix: the import is now under `TYPE_CHECKING` (it was only a type annotation).
   - Where: `services/response/loop.py:60`.

7. **Concurrent turns could read each other's pre-fetch results.**
   - Cause: the loop read the shared `ToolRunner.last_results` after `observe()`.
   - Fix: `observe_full()` returns this call's results.
   - Where: `services/tools/runner.py:91`.

8. **History compaction streamed its summary into the user's answer.**
   - Cause: `_compact_history` called the turn's publishing model, so the
     summary went out as `step.start`/`token`/`step` frames and the frontend
     showed it as a step.
   - Fix: side generations use the raw model.
   - Where: `services/response/context.py` (`fit` → `_compact`), with
     `raw_model` passed in from `turns.py`.

9. **A turn without a model started anyway, then died.**
   - Cause: `TurnRunner.start` checked only `model is None`, never `available`.
   - Fix: it raises "model unavailable", so `POST /api/turns` returns 503 as the
     contract says.
   - Where: `services/server/turns.py:160`.

10. **`web_fetch` cut pages at 4000 characters without saying so.**
    - Fix: `max_chars` (default 8000) and an explicit "page cut at N of M
      characters" note.
    - Where: `services/tools/builtin/web_fetch.py`.

11. **The old `shell` tool ran in the server's working directory (the repo)
    with the full environment.**
    - Cause: that included the API keys; there was no permission check, no
      deny-list, and a timeout that left child processes running.
    - Fix: workspace cwd, policy plus a second deny-list check in the tool,
      process-tree kill, scrubbed environment.
    - Where: `services/tools/builtin/shell.py`.

12. **The test suite collected 0 tests.**
    - Cause: `tests/` held only runnable scripts.
    - Fix: the `tests/test_agentic_*.py` suites and a `conftest.py` that puts the
      repo on `sys.path`.

13. **`ApiServer`'s default `SkillsStore` ignores `data_root`** (not fixed:
    `ApiServer.__init__` signature behaviour).
    - It always uses `~/.imurun/skills`. Found because a test created a skill in
      the real home; the test now injects a store, and the stray skill was removed.
