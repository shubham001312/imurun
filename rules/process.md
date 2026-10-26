# rules/process.md - Law for build agents on IMURUN

These rules bind every agent (human, AI, third party) that builds or
changes anything in this project. The model guard-rails are a DIFFERENT
system, planned separately - this file is not them.

## 1. Approval gate

- No module, function, screen, or key binding is written before the user
  approves it. Module-shape approval is not function approval.
- Propose, wait, build. Never build first and report after.

## 2. Decision log

- Every decision - the user's, the builder's, another model's - is informed
  to the user and recorded in docs/DECISIONS.md with ID, date, who, why.
- A decision that is not logged did not happen.

## 3. Evidence

- Evidence for any claim about behavior = A PASSING TEST (V7), run in CI.
- File paths, line numbers, and quotes are context. They never substitute
  for the test.

## 4. Re-verify, never trust memory

- Before acting on anything previously read or decided: re-read the actual
  current file. Context goes stale; other agents change things.
- If what you find differs from what you assumed: STOP, inform the user,
  do not continue on the old assumption.

## 5. Separate entities (V8)

- Each module is its own entity: its own tests, its own failure marking, its
  own end-to-end check. A module is proven by itself, not by a neighbor
  passing.

## 6. Failure marking (V9)

- If anything fails while running: MARK it - what failed, where, why - in
  the output, the logs, and the test report. Never silently skip, swallow
  or degrade past a failure without a visible mark.
- A skipped test must print as skipped, with its reason.

## 7. Test folder and CI

- test/ at the project root holds all tests. CI gating runs there: every
  change is checked end to end before it is accepted (V8, V7).
- No push without a green gate (user requirement).

## 8. Scope discipline

- Do not touch files outside the approved module.
- Do not rename, restructure, or delete existing docs or code without
  approval.

## 9. Instruction routing (V15, 2026-10-25)

Every instruction you give lands in exactly one of three cases:

- OUT OF DOMAIN - the instruction is NOT part of the module/process
  currently in flight: record it immediately and verbatim, KEEP WORKING,
  finish the current unit first, then start the new instruction. Nothing
  is dropped or half-applied; pending items stay visible to you.
- LIVE REDIRECT - you are monitoring the thinking blocks and directly
  instruct something else: the redirect takes effect immediately. The
  interrupted work is recorded as PENDING with its exact state so it
  resumes without loss.
- IN DOMAIN - the instruction concerns the current module: apply it now,
  on top of the current unit.

## 10. Verbatim retention (V16)

- Whatever you say is kept exactly as given. Problem statements are
  quoted, never paraphrased away; constraints are never dropped.
- Every instruction ever given is in exactly one state: DONE, QUEUED (with
  your exact words), or DECLINED with a verdict. Nothing falls out.
- Unclear instruction -> ask. Never guess, never forget.

## 11. Completion block (V17)

- Every unit of work presented to you carries a status block on the RIGHT
  side of the item:

      [x] completed - cross-checked against the codebase
      [ ] pending
      [!] failed - marked (V9)

- Before EVERY [x] tick: re-open the module/files actually changed, re-read
  them fresh, re-run the relevant test, and confirm the request's exact
  words are satisfied. A tick without this fresh cross-check is a process
  violation.
- "Should be fine" is never a tick. Failed verification -> [!] with what
  failed.
