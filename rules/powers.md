# rules/powers.md - Predefined powers (backend)

The powers/ folder holds functions the model can call DIRECTLY: web
search, web scraping, cross-validation.

- Redundancy: scraping has 2-3 INDEPENDENT methods (user: not one single
  option). If method 1 fails it triggers method 2 automatically; if all
  fail, the failure is reported (V9), not hidden.
- Every retry loop has a HARD EXIT POINT: attempt caps and a total time
  budget. No "loop loop loop" - an infinite loop is a bug by definition.
- Hallucination stop-caution: when the monitor sees the model looping or
  asserting unsupported claims, a stop-caution is fed back into the model's
  context (V5, V6: Python code decides this, the model does not police
  itself).
- Cross-validation: a claim found in one source must be checkable against
  another; unverifiable claims are marked as such in the output.
- Offline behavior: powers fail cleanly and say so - the turn continues
  without evidence rather than pretending it has some.
- Every power call is logged: method used, fallback triggered, outcome.
