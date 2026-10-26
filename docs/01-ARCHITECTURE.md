# IMURUN Rebuild - Architecture

## Layers

    frontend (JS)      TUI (terminal) + web UI (Tailwind + GSAP)
                       renders state, forwards input. Nothing else.
                            |
                            |  Agent API (Python)  - prompts, streams,
                            |  status, sessions
                            v
    backend (Python)   response/  agent loop, prompt handling, thinking
                              |    processing in code, sessions
                              v
                       model API (C++)  <-- user / third parties
                            |
                            v
    engine (C++)       server/   resident: model runs continuously,
                       model/     FISH: layer streaming, CPU/GPU choice

    services/ powers/ monitor/ providers/ sit beside response/ and are
    called by it; they never talk to the frontend directly.

## Rules of the shape (APPROVED)

- The heart is in the backend. The frontend only holds the running UI, the
  input capture, and the pages/sections the user sees.
- Two APIs (V10): the frontends call the Python agent API; the user and
  third parties can call the C++ model API. The agent API contract never
  changes when the user switches models - the server runs whatever model it
  wants behind it (V11).
- The model runs continuously on the server (resident), not started per
  request.
- When the server has no model or is stopped, the API answers with a clear
  status message that tells the user what is happening and how to proceed -
  never a raw error (V12).
- The frontend never learns which model is running, never talks to providers,
  files, or the OS. Every feature that would need that belongs in the
  backend instead (user direction, cross-checked).
- The model generates thinking; it never manages thinking. Thinking-block
  processing (split, collapse, stop-cautions) is Python code in response/
  and monitor/ (user decision, confirmed).
- Guardrails are enforced in code, not in prompt text: the model can ask,
  the code decides (V4).

## Turn flow

1. Frontend sends the user prompt to the agent API (token required).
2. response/ handles the request: builds context, applies settings
   (temperature, thinking level) for the NEXT request only if changed.
3. response/ asks server/ for tokens; server/ streams from the resident
   model.
4. Tool/power/skill calls execute in Python (services/, powers/) under
   code-enforced permissions.
5. monitor/ watches the loop: attempt caps, exit points, stop-cautions.
6. Answer streams back; one active turn per session, concurrent callers
   queue (rules/api.md).

## Concurrency model (rules/api.md is the contract)

One resident model answers one request at a time: FIFO queue, per-client
cap, drain before model swap, disconnect-safe streaming, per-OS-user
instances, total-load resource governor.
