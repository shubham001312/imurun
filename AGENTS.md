# Agents

This repository's orientation file is `CLAUDE.md` (same directory). It applies to
every CLI agent and every human: project map, build/test commands, ownership
scopes, conventions and the list of things that are not yet real.

Quick rules:

1. Read `CLAUDE.md`, then the `docs/` page for the area you are changing.
2. Stay inside one ownership scope; cross-scope needs go into the matching
   `*-REQUESTS.md` file.
3. Engine changes must pass `fish_test_m1` and `fish_test_m2`; server changes must
   keep `python -m py_compile` clean and the engine scripts in `tests/` working.
4. Record what you changed and why in the area's `CHANGELOG.md`, and each bug as
   symptom → cause → fix with a `file:line`.
