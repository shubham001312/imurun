# rules/ui.md - Frontend law

- Frontend holds ONLY what the user sees: pages, sections, input capture,
  visuals. No model knowledge, no provider calls, no file or OS access
  from the frontend - every such feature belongs in the backend.
- Structure (layout v4):
  frontend/sections/ - hierarchical modules: different pages, parts and
    sections that deal with the user directly.
  frontend/input/    - input capture: mouse, keyboard, special function
    keys. Every key gets exactly ONE defined ability (the key map is
    designed with the user, later).
- Two surfaces, one contract: the TUI and the web UI both speak the agent
  API only, so they behave identically.
- Element-by-element approval: every screen, every input binding, every
  visual element is designed and approved with the user before it is
  built ("we will design element 1 by 1, every inch").
- First-run flow (V1): welcome banner + model chooser - a separate design,
  approved later.
- Status screens (V12) are designed states, not error dumps: no model,
  stopped, busy, swapping each get a proper message and a next step.
- Failures are visible (V9): if something breaks while running, the UI
  marks it - what failed and where - never a silent skip.
- ASCII punctuation in terminal output (user answer-style rule).
