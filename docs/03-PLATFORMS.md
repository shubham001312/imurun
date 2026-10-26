# IMURUN Rebuild - Platforms

One product, per-OS artifacts. A single binary for all three OSes does not
exist; the product is single, the artifacts are per OS.

## Per-layer strategy

| Layer          | Strategy |
|----------------|----------|
| C++ engine     | one source, per-OS CI builds: Windows x64, macOS universal2 (Intel + Apple Silicon in one file), Linux x64 built against old glibc |
| Python service | bundled runtime per OS. Never system Python (macOS removed it; Linux distros block installs) |
| JS frontends   | web UI = plain files served by the backend (zero install); TUI = packaged per-OS executable |

## macOS conflicts and how each is cleared

| Conflict | Cause | Clearance |
|----------|-------|-----------|
| "Cannot be opened" block | unsigned app (V3: $0 budget) | right-click -> Open, then Settings -> Open Anyway; documented with pictures |
| Signed but still killed | hardened runtime checks every loaded binary | n/a (unsigned path): bypass docs cover it |
| Weird paths from DMG | downloads are mounted read-only, apps get relocated | install target Application Support paths under the user, never next to the binary |
| Python missing | removed from macOS since 12.3 | bundled runtime |
| Slow on Apple Silicon | Intel binary under Rosetta | universal2 native build |
| Permission popups | TCC privacy gates | ask only when the feature is used; document Full Disk Access |
| Background server dies | logout/sleep | LaunchAgent, auto-restart on wake |
| Works on Linux dev, breaks on Mac | case-insensitive APFS, path separators | path rules in rules/, clean-room VM test |

## Windows

- SmartScreen and Defender heuristics (unsigned + self-updating binaries
  trip them): documented More info -> Run anyway; scheduled-task swap for
  self-update (proven in 0.1.11).
- Per-user install without admin; UTF-8 console forced at startup.

## Linux

- Build on old glibc for distro range; no systemd dependency - plain
  background process, user level only; no root required.

## Verdicts on record

- V1: no runnable model bundled; first open shows the chooser; model download
  is a separate user-invoked step. The ~1 GB TUI/monitor model is parked.
- V2: SSD streaming is bounded by bandwidth physics; expectations set in
  docs/02-NAMING.md.
- V3: $0 signing budget - unsigned builds with documented bypass steps.
