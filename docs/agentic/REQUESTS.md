# Requests from the agentic layer to other owners

Nothing in these scopes was edited by the agentic work. The server side
already works without any of these items; each one would make it better.

## Frontend (`frontend/design-lab/**`)

1. **Show tool activity live (P1).** Subscribe to `tool.call`, `tool.result`,
   `agent.start`, `agent.end` and `skill.used` (add them to `EVENT_TYPES` in
   `api.js`), and render a compact row per call in the thinking panel: tool
   name, short args, ok/failed, latency. `step` events now carry
   `kind: "tool"` with readable text, so the current UI already degrades
   gracefully.
2. **Approval cards for tools (P1).** On `tool.approval_needed`
   `{approval_id, tool, args, reasons, expires_in, turn_id}`, show the existing
   approval card (`toast.js` `approval: true`) with Allow / Deny and a
   "Remember for this session" checkbox, then
   `POST /api/tools/approvals/{approval_id}` `{approved, remember}`. Close the
   card on `tool.approved` / `tool.denied`, which also fire on timeout or
   Stop. After a reload, `GET /api/tools/approvals` restores pending cards.
   Without this, writes and shell in the default mode are denied at once
   ("no client is connected") or time out after 180 s.
3. **Permissions panel.** Bind `GET/POST /api/permissions`: a mode selector
   (`ask` / `allow-reads` / `allow-all`), a workspace folder field
   (`workspaceRoot`, absolute path), and the existing toggles. The settings
   copy "Every write opens an approval card" is true only in `ask` and
   `allow-reads`.
4. **Skills.** A `/` autocomplete in the composer from `GET /api/skills`
   (`/<id>`), and a "used skill" chip from `turn.end.skills`.
5. **Manual tool runner (optional).** `POST /api/tools/run` returns
   `403 {"decision":"ask"}`. Show the card, then repeat with `approved: true`.

## Settings defaults (`services/server/settings.py`, server owner)

6. Add to `DEFAULTS["permissions"]`: `"mode": "allow-reads"`,
   `"workspaceRoot": ""`, `"approvalTimeout": 180`; and add
   `DEFAULTS["agent"] = {"maxSteps": 6}`. Effective defaults are applied in
   `services/tools/permissions.py` already, but `GET /api/settings/permissions`
   does not list these keys until they are first written.
7. `ApiServer`'s fallback `SkillsStore(mark=...)` ignores `data_root` and
   always uses `~/.imurun/skills`. This is harmless in production (`app.py`
   injects one) but a trap for tests.

## Engine (`model/**`)

8. **Tool role per template.** Tool results are sent as `user` messages,
   because the role string is passed verbatim into the template. If
   `fish_chat_prompt` mapped a `tool` role to `ipython` (Llama 3), `tool`
   (ChatML/Qwen) and `user` (DeepSeek/Gemma/Phi-3/Mistral), the loop could
   switch to `role: "tool"`, and Llama 3.1/Qwen would see their trained format.
9. **Stop sequences.** A per-request stop list (for example the closing
   ```` ``` ```` after a ```` ```tool ```` block, `</tool_call>`,
   `<|eom_id|>`) would end tool steps early and save decode time. The parser
   already discards text after the call.
10. **Token counting.** An `EngineModelClient.count_tokens(messages)` (or
    `fish_chat_prompt` token count) would replace the characters/3.5 estimate
    in `services/response/context.py`.
11. **Special-token passthrough.** Confirm whether `<|python_tag|>` /
    `<|eom_id|>` reach the decoded text for Llama 3.1. The parser handles both
    cases.

## Setup / launcher (`start.py`, `install.py`, docs/setup)

12. **Web search key.** The keyless DuckDuckGo endpoint returned a bot-check
    page on this machine (2026-10-31), so live search fails without a key.
    Please surface `IMURUN_BRAVE_API_KEY` / `IMURUN_TAVILY_API_KEY` /
    `IMURUN_SERPER_API_KEY` / `IMURUN_SEARXNG_URL` in setup docs (and,
    ideally, a Settings field the server reads into the environment at
    start). `web_search` now reports the failure honestly instead of
    returning "nothing".

## Voice (`services/voice/**`, added 2026-11-02 by the voice owner)

13. ~~**Answer in the language the user spoke.**~~ Done 2026-11-02: `POST /api/turns {language}` -> `Turn.language` -> `AgentLoop(language=)` -> `voice.languages.answer_directive` line after the house style (`tests/test_turn_language.py`). Original text: `POST /api/voice/transcribe`
    returns `language` (ISO 639-1, e.g. `hi`) and `answer_language` (the same
    code when `voice.replyInSameLanguage` is on, else `""`). The shell will
    send it on the turn: `POST /api/turns {session_id, text, language: "hi"}`.
    Needed (server + loop owners, about four lines):
    - `services/server/api.py` `/api/turns`: pass `language=str(payload.get("language") or "")`
      to `server._turns.start(...)`; `TurnRunner.start` stores it on the turn
      and hands it to the loop.
    - `services/response/loop.py` `_initial_messages`: after
      `parts.append(DEFAULT_DIRECTIVE)` add
      `parts.append(answer_directive(self._language))` where
      `from ..voice.languages import answer_directive` (stdlib-only module,
      returns `""` for English/unknown, else e.g.
      `- The user is speaking Hindi (हिन्दी). Answer in Hindi (हिन्दी), in its own script.`).
    Not done by the voice owner because it touches three files in two other
    scopes; until then the reply language depends on the model following
    the user's script.
