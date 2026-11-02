# `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\`

## Purpose

Guardrails G1–G7, enforced in code rather than in prompt text. The package
measures the machine (`probe`), converts that into safe budgets (`limits`),
decides every admission and action (`check_start`, `check_request`,
`check_action`, `require`), serializes memory-heavy work (`Governor`), stops
everything on demand (`KillSwitch`), confines outbound traffic (`NetworkGuard`),
refuses placeholder secrets (`validate_key`), and records every decision
(`Audit`, `JsonlSink`). Nothing in the package is allowed to raise at a caller
because a probe, sink or mark failed; the failure is recorded instead.

## Public API

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\profile.py`

| Symbol | Signature | Notes |
|---|---|---|
| `DEFAULT_DEADLINE` | `3.0` | Whole-probe budget in seconds. |
| `GpuInfo` | `@dataclass(frozen=True) GpuInfo(present: bool, vendor: str \| None = None)` | Vendors: `nvidia`, `amd`, `intel`, `apple`, `other`. |
| `MachineProfile` | `@dataclass(frozen=True) MachineProfile(ram_total, ram_available, cpu_cores, disk_free, gpu, errors=())` | Byte counts; `errors` is a tuple of `"<part>: <reason>"`. |
| `probe` | `probe(*, memory=None, disk=None, gpu=None, path=None, model_dir=None, deadline=DEFAULT_DEADLINE) -> MachineProfile` | Injectable parts for tests; never raises. |

`probe` runs four parts (memory, CPU, disk, GPU) each inside the remaining
deadline using `_within` (a daemon thread + `join`). A part that fails or times
out appends to `errors` and the profile is returned with what is known. Disk is
measured on `model_dir`, else `path`, else `~/.imurun`, walking up to the nearest
existing directory. `_PHYSICAL_CORES` caches the CPU result process-wide.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\limits.py`

| Symbol | Signature | Notes |
|---|---|---|
| `GB`, `MB` | `1024**3`, `1024**2` | |
| `Limits` | `@dataclass(frozen=True) Limits(mode, max_model_ram, max_threads, min_free_disk, max_request_bytes, request_timeout, kv_bytes_per_token, compute_overhead)` | |
| `limits` | `limits(profile: MachineProfile, mode: str = "conservative") -> Limits` | `mode` must be `conservative` or `full`; raises `ValueError` otherwise. |

| Mode | RAM share | Threads | Min free disk | Request cap | Timeout |
|---|---|---|---|---|---|
| `conservative` | 60 % of total RAM | `cores - 1` (min 1) | 2 GB | 8 MB | 120 s |
| `full` | 90 % of total RAM | all cores (min 1) | 1 GB | 64 MB | 300 s |

`KV_BYTES_PER_TOKEN = 128 KiB`, `COMPUTE_OVERHEAD = 512 MB`. Budgets use
`ram_total` (falling back to `ram_available`), never the transient free figure.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\decisions.py`

| Symbol | Signature | Notes |
|---|---|---|
| `Decision` | `@dataclass(frozen=True) Decision(kind: str, rule: str, reasons: tuple[str, ...] = ())` | `kind` is `allow`/`ask`/`deny`; `Decision.allowed` is `kind == "allow"`. |
| `PermissionRequired` | `class PermissionRequired(PermissionError)` | Raised when an `ask` is attempted without a grant. |
| `Grant` | `@dataclass(frozen=True) Grant(tools: frozenset[str], roots=(), hosts=frozenset())` | `Grant.full(project_root, user_data_root)` includes `file_read`, `file_write`, `file_delete`, `shell`, `network`. |
| `Grant.intersect` | `(other: Grant) -> Grant` | Only shrinks: tools/roots/hosts intersected. |
| `ModelRequest` | `@dataclass(frozen=True) ModelRequest(model_bytes: int, context_tokens: int = 8192, threads: int = 1)` | |
| `require` | `require(decision: Decision, granted: bool = False) -> Decision` | `deny` always raises; `ask` raises unless `granted`. |
| `check_start` | `(request, limits, profile) -> Decision` | Admission for a model start. |
| `check_request` | `(size: int, elapsed: float, limits) -> Decision` | Per-request byte and time caps. |
| `check_action` | `(action: dict, *, project_root, user_data_root, allowed_hosts=(), grant=None, protected_roots=()) -> Decision` | File, shell and network actions. |

`check_start` order: negative/zero inputs → `deny` (`G1-governor`); probe errors
→ `ask` (`G2-hardware`); model larger than `disk_free - min_free_disk` → `deny`
(`G4-disk`); resident need (model + KV cache + overhead) over twice the RAM
budget → `ask` (`G1-governor`, streaming opt-in); over budget or too many
threads → `ask`; otherwise `allow`.

`check_action` kinds and outcomes:

| `kind` | Outcome |
|---|---|
| `file_read`, `file_write` | `deny` if no path, protected (guardrail source, `.git`, `.env*`, `.netrc`, `.npmrc`, `.pypirc`, `.git-credentials`, `.htpasswd`, `protected_roots`), a system path, or outside the roots; else `allow`. |
| `file_delete` | Same checks, then `ask` (`G3-action`). |
| `shell` | `deny` if empty or matching `DANGEROUS_COMMANDS`; else `ask`. |
| `network` | `allow` when the host is in the grant hosts or `allowed_hosts`; else `ask` (`G5-network`). |
| `registry`, `device`, `disk` | Always `deny`. |
| anything else | `deny` with the unknown kind in the reason. |

`SYSTEM_PREFIXES` is computed per OS at import; `DANGEROUS_COMMANDS` is a tuple
of compiled patterns (recursive `rm`, `mkfs`, `dd of=/dev/`, `format C:`,
`del /f`, `diskpart`, `bcdedit`, shutdown, execution-policy bypass, fork bomb).

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\governor.py`

| Symbol | Signature | Notes |
|---|---|---|
| `Ticket` | `@dataclass Ticket(id, weight, client, expires_at, released=False)` | `expires_at` is on the `time.monotonic()` clock. |
| `Governor.__init__` | `(capacity: int, per_client: int \| None = None, lease_seconds: float = 300.0, mark=None)` | All three numeric arguments must be positive; raises `ValueError`. |
| `Governor.capacity` | `-> int` (property) | |
| `Governor.status` | `() -> dict` | `{capacity, used, per_client, queued:[client]}`. |
| `Governor.acquire` | `(weight=1, client="default", timeout=None) -> Ticket \| None` | FIFO by arrival; `None` on timeout. Raises `ValueError` for `weight <= 0`, `weight > capacity`, or `weight > per_client`. |
| `Governor.release` | `(ticket: Ticket \| None) -> None` | Idempotent; a ticket from another governor is ignored. |

Expired leases are reclaimed on every `acquire` and marked `G1-lease`, so a
crashed caller cannot hold capacity forever.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\killswitch.py`

| Symbol | Signature | Notes |
|---|---|---|
| `StopResult` | `@dataclass(frozen=True) StopResult(id, name, ok, error=None)` | |
| `KillSwitch.__init__` | `(mark=None)` | |
| `KillSwitch.register` | `(name: str, stop: Callable[[], object]) -> int` | Returns a monotonic id; raises `TypeError` for a non-callable. |
| `KillSwitch.unregister` | `(stop_id: int) -> bool` | Marks an unknown id; returns whether it was removed. |
| `KillSwitch.fire` | `() -> list[StopResult]` | Calls every stop in registration order; one failure does not skip the rest. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\audit.py`

| Symbol | Signature | Notes |
|---|---|---|
| `Event` | `@dataclass(frozen=True) Event(at: float, rule: str, subject: str, why: str)` | |
| `Audit.__init__` | `(sink: Callable[[Event], object] \| None = None, capacity: int = 500)` | `capacity` must be positive. |
| `Audit.sink_errors` | `-> int` (property) | Monotonic counter; survives ring trimming. |
| `Audit.mark` | `(rule, subject, why) -> Event` | Appends to a bounded ring, then calls the sink. A raising sink increments `sink_errors` and appends a `G7-sink` event. |
| `Audit.tail` | `(limit: int = 20) -> list[Event]` | Oldest→newest; `[]` for `limit <= 0`. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\sink.py`

| Symbol | Signature | Notes |
|---|---|---|
| `JsonlSink.__init__` | `(path, max_bytes: int = 5_000_000, backups: int = 3)` | `max_bytes > 0`, `backups >= 1`; creates the parent folder. |
| `JsonlSink.path` | `-> Path` (property) | |
| `JsonlSink.__call__` | `(event: Event) -> None` | One JSON line, `flush` + `os.fsync`, rotation first. |
| `default_sink` | `(path: Path \| str \| None = None) -> JsonlSink` | Defaults to `~/.imurun/logs/audit.jsonl`. |

Rotation renames `.1 … .{backups}` and drops the oldest. Not thread-safe across
processes; guarded by an in-process `threading.Lock`.

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\network.py`

| Symbol | Signature | Notes |
|---|---|---|
| `LOOPBACK_HOSTS` | `frozenset({"localhost", "127.0.0.1", "::1"})` | |
| `NetworkDenied` | `class NetworkDenied(PermissionError)` | |
| `host_of` | `(url: str) -> str` | Only `http`/`https`; raises `NetworkDenied` otherwise. |
| `HostGrant` | `@dataclass(frozen=True) HostGrant(host: str, expires_at: float)` | |
| `NetworkGuard.__init__` | `(mark=None, clock=time.monotonic)` | |
| `NetworkGuard.grant` | `(host: str, ttl: float = 300.0) -> HostGrant` | Empty host or `ttl <= 0` → `ValueError`. |
| `NetworkGuard.revoke` | `(host: str) -> None` | |
| `NetworkGuard.pin` | `(provider: str, hosts) -> None` | Requires a provider and at least one non-empty host. |
| `NetworkGuard.check` | `(url: str) -> str` | Loopback or a live grant; else `NetworkDenied` + mark. |
| `NetworkGuard.check_for` | `(provider: str, url: str) -> str` | Host must be pinned to that provider. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\keys.py`

| Symbol | Signature | Notes |
|---|---|---|
| `MIN_KEY_LENGTH` | `8` | |
| `PLACEHOLDERS` | `frozenset` | `changeme`, `your-key`, `todo`, `none`, `secret`, `password`, … |
| `PLACEHOLDER_PREFIXES` | `tuple` | `your-`, `replace-`, `insert-`, `paste-`. |
| `InvalidKey` | `class InvalidKey(ValueError)` | Message never contains the key. |
| `validate_key` | `(key) -> str` | Trims; rejects non-strings, short keys, placeholders, single repeated characters. |
| `redact` | `(key: str \| None) -> str` | `(none)`, `****`, or `****` + last 4. |
| `scrub` | `(text: str, key: str \| None) -> str` | Replaces an accidental occurrence with the redacted form. |

### `C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\services\safety\__init__.py`

Re-exports `Audit`, `Decision`, `Event`, `GpuInfo`, `Governor`, `Grant`,
`KillSwitch`, `Limits`, `MachineProfile`, `ModelRequest`, `PermissionRequired`,
`StopResult`, `Ticket`, `check_action`, `check_request`, `check_start`, `limits`,
`probe`, `require`. It does **not** export `JsonlSink`, `default_sink`,
`NetworkGuard`, `NetworkDenied`, `HostGrant`, `host_of`, `validate_key`,
`InvalidKey`, `redact` or `scrub`; import those from their modules.

## Connections

| Importer | Uses |
|---|---|
| `services/connectors/gate.py` | `Decision`, `PermissionRequired`, `require`. |
| `services/sessions/session.py` | `PermissionRequired` for cross-session reads. |
| `services/server/api.py` | `PermissionRequired` (connector actions). |
| `services/server/turns.py` → `services/response/loop.py` | `kill` (a `KillSwitch`) and an audit object with `.mark()`. |
| `services/server/app.py` | Accepts `kill`, `persona`, `mark`; no direct safety import. |

No HTTP route calls this package directly. Guardrails surface through the API's
`mark` callback (`_record` → `mark("G3-api", subject, why)`), through
`ConnectorGate`, and through the `403` response for a refused connector action.
Tests: `test/services/safety/` (audit, audit_sink, decisions, governor, keys,
killswitch, limits, network, profile).

## Invariants

- A decision is never `allow` because the model asked for it; only `require()`
  with an explicit grant turns `ask` into permission.
- Protected paths (guardrail source, `.git`, `.env*`, credential dotfiles) and
  system prefixes are `deny`, not `ask`.
- `Audit.mark` and every `mark` callback must never raise to the caller; a broken
  sink is counted (`sink_errors`) and recorded.
- `Governor` capacity is measured in MB-equivalent weight; a release is
  idempotent and a foreign ticket cannot free capacity.
- `KillSwitch.fire` runs all registered stops even when one fails, and marks both
  success and failure.
- `probe` never blocks longer than `deadline`; partial results are returned with
  `errors` populated.
- `NetworkGuard` allows loopback unconditionally; any other host needs a live
  grant or a provider pin.
- `validate_key` messages, `redact` output and `scrub` output never contain the
  key body.
