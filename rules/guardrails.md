# rules/guardrails.md - Device safety (the first rules to build)

These guard-rails prevent damage to the machine IMURUN runs on. The user
ordered them FIRST, before any feature code. They are ENFORCED IN CODE
(V4): the model can ask, the code decides.

NOTE: guard-rails for the MODEL's behavior while running are a separate,
later design (user). This file is device safety only.

- G1 Resource governor
  Never exceed safe RAM / CPU / disk defaults. Using ALL resources of the
  machine requires explicit manual opt-in by the user. The governor bounds
  total system load, so parallel callers cannot multiply past it.

- G2 Hardware policy
  CPU-only machines must always run everything that matters. The GPU is
  used only when it is genuinely more capable for the task than the CPU.
  The engine decides per capability; the user can override the choice.

- G3 Action gating
  Destructive actions are denied by default: system directories, disk and
  registry writes, killing system processes. Dangerous-but-legitimate
  actions ask permission. The permission check is in the executing code,
  not in a prompt.

- G4 Disk and thermal
  Free-space pre-flight before any download; refuse to start what cannot
  fit. Sustained-load caps so a long run does not cook the machine.

- G5 Network
  Loopback only for the local APIs (rules/api.md). Offline-first: every
  core path works with no network. No telemetry, ever. API keys come from
  the environment only and are never written to files.

- G6 Kill switch
  One command stops everything: streams, queue, engine, background
  services. Every action is logged so "what happened" is always answerable.

- G7 Failure marking (V9)
  A guard-rail that fires is MARKED in the UI and the log - which rule,
  what was blocked. A guard-rail failure is never silent.
