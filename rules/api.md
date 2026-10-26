# rules/api.md - Local API contract

Approved draft (user: "Approve API rules draft", 2026-06-11).

## Shape (V10)

- Agent API (Python, response/): what both frontends call - prompts,
  streams, status, sessions. Requires a per-session token.
- Model API (C++ server/): what the user and third parties call directly.
- V11: the agent API contract NEVER changes when the user swaps models.
- V12: no model loaded / server stopped -> a clear status message saying
  what is happening and how to proceed. Never a raw error or stack trace.

## Nine multi-client rules

1. Loopback bind only (127.0.0.1), Host header check, no CORS. The API can
   run shell commands - an unauthenticated door is remote code execution for
   any local process and any web page.
2. Per-session auth token: memory + user-permission file only, never in the
   repo, never on disk in plaintext readable by others.
3. One resident model answers one request at a time: FIFO queue, per-client
   cap, queued status visible to the caller.
4. One active turn per session. A second concurrent send gets a clear busy
   answer, not interleaved output.
5. Model swap drains active streams first, or the caller gets a documented
   cancel answer. Never yank a model from under a generation.
6. Client disconnect mid-stream is normal: abort the stream, save the
   session, server keeps serving.
7. One instance per OS user: separate data directories, no cross-user read.
8. Body size caps, timeouts, request validation - malformed input answers
   with a proper message, never a crash.
9. The resource governor bounds TOTAL load (rules/guardrails.md G1), not
   per-caller load - calling twice cannot bypass it.
   Settings changes (temperature, thinking level) apply on the NEXT request,
   never mid-stream.

## Status vocabulary (design input for the status screen)

- "no model loaded" -> how to load one (chooser / command)
- "server stopped" -> how to start it
- "busy" -> position in queue
- "model swapping" -> when it will answer
