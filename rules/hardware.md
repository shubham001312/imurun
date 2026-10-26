# rules/hardware.md - Hardware policy

- CPU first: a machine with no GPU must run the full core experience.
  Nothing that matters may be GPU-only (ties to G2).
- The engine decides CPU vs GPU per task: GPU only when it is genuinely
  more capable for THAT task than the CPU (user direction).
- The user can always override the engine's choice; the override is a
  visible setting, not a config-file secret.
- Hardware detection is time-boxed (bounded probe timeouts), cached, and
  runs in the background - never blocks startup or input.
- FISH streaming (docs/02-NAMING.md): weights stream from disk within the
  bandwidth limit of V2; expectations are set by the design doc, not by
  claims the machine cannot honor.
