# Audit register

A flaw is closed only when its proof test passes. The built tree is audited
again after every repair batch; this file is the current register.

## Original audit (delivered in conversation, 80 flaws / 1430 points)

| Severity | Original | Closed | Open |
|---|---|---|---|
| Critical | 8 | 5 | 3 — C-03, C-04, C-08 |
| High | 30 | 13 | 17 |
| Medium | 34 | 11 | 23 |
| Low | 8 | 1 | 7 |
| **Total** | **80** | **30** | **50** |

The per-flaw sheet for the original audit is not in the repository. The 50
open IDs cannot be closed one by one until that sheet is provided; the three
remaining criticals are known by id (C-03, C-04, C-08).

## Re-audit of the built tree (2026-10-26)

Scope: `services/safety/`, `services/prompts/`, `services/sessions/`,
`services/skills/`, `services/connectors/`, `services/persona/`, `test/`.
Every flaw below was found by reading the code, then fixed with a test.

| ID | Severity | Points | Flaw | Proof test | Status |
|---|---|---|---|---|---|
| AF-01 | High | 25 | Provider identifiers were interpolated into URL paths (repo, service, zone, record id, message id, design id, project) - path traversal / parameter confusion | `ProviderInjectionTests` (9 cases) | fixed |
| AF-02 | High | 20 | Gmail and Slack query values were interpolated raw - extra parameters could be injected | `test_a_gmail_query_cannot_add_parameters` | fixed |
| AF-03 | High | 20 | Negative model size, context, threads, request size or elapsed time bypassed every budget | `InputValidationTests` (5 tests) | fixed |
| AF-04 | High | 15 | `Governor.release()` accepted a ticket from another governor and corrupted `used` | `GovernorTicketOwnershipTests` | fixed |
| AF-05 | Medium | 10 | Session ids had 2 bytes of entropy and a collision could overwrite a session file | `SessionAllocationTests.test_a_colliding_id_is_replaced` | fixed |
| AF-06 | Medium | 10 | Allowed cross-session reads were not audited (only refusals were) | `test_an_allowed_cross_session_read_is_marked` | fixed |
| AF-07 | Medium | 8 | Cancelled queued prompts vanished without a trace | `CancelledHistoryTests` | fixed |
| AF-08 | Medium | 8 | A flat `<id>.md` skill and a `<id>/SKILL.md` skill could share an id | `test_a_flat_skill_file_blocks_the_same_id` | fixed |
| AF-09 | Medium | 8 | PersonaStore read an unbounded file into memory | `test_an_oversized_persona_file_returns_an_error` | fixed |
| AF-10 | Low | 5 | A negative machine profile produced negative budgets | `LimitsSafetyTests` | fixed |
| AF-11 | Low | 5 | Unregistering an unknown kill-switch id was silent | `KillSwitchUnregisterTests` | fixed |
| AF-12 | Low | 4 | `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.htpasswd` were not protected secrets files | `test_secret_files_are_protected` | fixed |
| AF-13 | Low | 6 | A failed `chmod` was swallowed, so a private file could silently be non-private | `test_a_failed_chmod_is_not_ignored_on_posix` (persona, sessions, skills) | fixed |

**Score: 144 points → 0 open. 13 of 13 fixed, each with a passing test.**
