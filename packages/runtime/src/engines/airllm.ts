/**
 * The deep engine: AirLLM, behind the same interface as llama.cpp.
 *
 * AirLLM streams a model's layers from disk one at a time, so a 32B or 70B
 * model runs on a machine that could never hold it. It is slow - minutes for
 * an answer, not seconds - and it does not really stream, so the reply
 * arrives in one piece. Those are real costs, and the interface says so.
 *
 * The work happens in Python, because that is where AirLLM lives. This file
 * is the thin, honest bridge: it writes the server (which ships inside the
 * binary, so an npm install needs no extra files), starts it, and talks to it
 * over the same OpenAI-shaped HTTP the rest of IMURUN already speaks.
 *
 * The server source is imported as text. That matters: a compiled binary has
 * no source tree next to it, and a `pip install` user has no server file
 * either. Embedding it means one artifact works everywhere.
 */
import { closeSync, existsSync, openSync } from "node:fs"
import { mkdir, writeFile } from "node:fs/promises"
import { join } from "node:path"
import { imurunDir, logger } from "@imurun/util"
import AIRLLM_SERVER from "../../../../python/imurun/airllm_server.py" with { type: "text" }
import type { Engine, EngineHealth, EngineHandle, EngineStartOptions } from "./base"
import { isProcessAlive, killProcess } from "./base"

const log = logger.child("airllm")

export interface PythonCommand {
  command: string
  /** Arguments that go before the script, e.g. `-3` for the Windows launcher. */
  prefix: string[]
}

/** Where the deep engine keeps its virtualenv, its server and its notes. */
export function airllmHome(): string {
  return join(imurunDir("engines"), "airllm")
}

export function airllmVenvPython(home = airllmHome()): string | undefined {
  const candidates =
    process.platform === "win32"
      ? [join(home, "venv", "Scripts", "python.exe")]
      : [join(home, "venv", "bin", "python3"), join(home, "venv", "bin", "python")]
  return candidates.find((candidate) => existsSync(candidate))
}

/**
 * Finds a Python interpreter.
 *
 * The engine's own virtualenv wins, then an explicit setting, then whatever
 * the machine has. `py` on Windows takes `-3`; the others do not.
 *
 * A candidate that cannot actually run is skipped rather than returned: the
 * Microsoft Store's `python` alias is a zero-byte stub that spawns into
 * EFTYPE, and a low-end machine is exactly where that stub is the only
 * "python" on PATH. One cheap `--version` probe per candidate, cached.
 */
export function findPython(explicit?: string): PythonCommand | undefined {
  if (explicit && existsSync(explicit)) return { command: explicit, prefix: [] }

  const venv = airllmVenvPython()
  if (venv) return { command: venv, prefix: [] }

  const candidates: PythonCommand[] = []
  const python = Bun.which("python")
  if (python) candidates.push({ command: python, prefix: [] })
  const python3 = Bun.which("python3")
  if (python3) candidates.push({ command: python3, prefix: [] })
  const py = Bun.which("py")
  if (py) candidates.push({ command: py, prefix: ["-3"] })

  for (const candidate of candidates) {
    if (runsPython(candidate)) return candidate
  }
  return candidates[0]
}

/** Whether `python --version` runs, cached for the life of the process. */
const pythonProbes = new Map<string, boolean>()

function runsPython(candidate: PythonCommand): boolean {
  const cached = pythonProbes.get(candidate.command)
  if (cached !== undefined) return cached
  let usable = false
  try {
    const result = Bun.spawnSync([candidate.command, ...candidate.prefix, "--version"], {
      stdout: "ignore",
      stderr: "ignore",
      stdin: "ignore",
      timeout: 5000,
    })
    usable = (result.exitCode ?? 1) === 0
  } catch {
    usable = false
  }
  pythonProbes.set(candidate.command, usable)
  return usable
}

/** Writes the bundled server next to the engine, and returns its path. */
export async function ensureAirllmServer(): Promise<string> {
  const home = airllmHome()
  await mkdir(home, { recursive: true })
  const path = join(home, "airllm_server.py")
  await writeFile(path, AIRLLM_SERVER, "utf8")
  return path
}

/** Asks the server what it is doing right now. */
export interface AirllmStatus {
  ok: boolean
  stage?: string
  detail?: string
  percent?: number | null
  error?: string | null
  model?: string
  loaded?: boolean
  requests?: number
  uptimeMs?: number
  lastLatencyMs?: number | null
}

export async function probeAirllm(
  port: number,
  timeoutMs = 2500,
): Promise<AirllmStatus | undefined> {
  try {
    const response = await fetch(`http://127.0.0.1:${port}/health`, {
      signal: AbortSignal.timeout(timeoutMs),
    })
    if (!response.ok) return { ok: false, stage: "error", detail: `HTTP ${response.status}` }
    const body = (await response.json()) as AirllmStatus & { status?: string }
    return { ...body, ok: body.status === "ok" || body.stage !== "error" }
  } catch {
    return undefined
  }
}

/**
 * The command line that runs the server: the interpreter first, its launcher
 * prefix (`-3` for the Windows `py` launcher), then the script.
 *
 * The interpreter used to be left out of this array, and the spawn tried to
 * execute the Python *script* as the program — uv_spawn answered EFTYPE
 * ("inappropriate file type or format") on every deep-model turn. The args
 * are built here, as a pure function, so a test can hold the line.
 */
export function buildAirllmArgs(
  python: PythonCommand,
  server: string,
  options: { modelRepo: string; modelName?: string; port: number; compression?: string; maxNewTokens?: number },
): string[] {
  return [
    python.command,
    ...python.prefix,
    server,
    "--model",
    options.modelRepo,
    "--name",
    options.modelName ?? options.modelRepo.split("/").pop() ?? options.modelRepo,
    "--port",
    String(options.port),
    "--compression",
    options.compression ?? "4bit",
    "--max-new-tokens",
    String(options.maxNewTokens ?? 1024),
  ]
}

export class AirLlmEngine implements Engine {
  readonly id = "airllm"
  readonly displayName = "AirLLM (deep, streamed from disk)"

  constructor(private readonly pythonOverride?: string) {}

  async isAvailable(): Promise<{ available: boolean; detail?: string }> {
    const python = findPython(this.pythonOverride)
    if (!python) {
      return {
        available: false,
        detail:
          "Python 3 is not on PATH. Install it from python.org, then run `imurun setup --deep`.",
      }
    }
    const venv = airllmVenvPython()
    return {
      available: true,
      detail: venv
        ? `deep engine ready (${venv})`
        : `Python found (${python.command}); airllm and torch are installed on demand`,
    }
  }

  async start(options: EngineStartOptions): Promise<EngineHandle> {
    if (!options.modelRepo) {
      throw new Error(
        "The deep engine needs a model: a Hugging Face repo (Owner/Name) or a local directory.",
      )
    }
    const python = findPython(this.pythonOverride)
    if (!python) {
      throw new Error("Python 3 is required for the deep engine. Install it, then `imurun setup --deep`.")
    }

    const server = await ensureAirllmServer()
    const args = buildAirllmArgs(python, server, {
      modelRepo: options.modelRepo,
      modelName: options.modelName,
      port: options.port,
      compression: options.compression,
      maxNewTokens: options.maxNewTokens,
    })

    log.info(`starting airllm on port ${options.port} for ${options.modelRepo}`)
    let logHandle: number | undefined
    if (options.logFile) {
      try {
        logHandle = openSync(options.logFile, "a")
      } catch {
        logHandle = undefined
      }
    }

    let proc: ReturnType<typeof Bun.spawn>
    try {
      proc = Bun.spawn(args, {
        stdout: logHandle !== undefined ? logHandle : "ignore",
        stderr: logHandle !== undefined ? logHandle : "ignore",
        stdin: "ignore",
        windowsHide: true,
        env: { ...process.env },
      })
    } finally {
      if (logHandle !== undefined) {
        try {
          closeSync(logHandle)
        } catch {
          // the child owns the handle now
        }
      }
    }
    try {
      proc.unref()
    } catch {
      // best effort on older runtimes
    }

    // The server binds immediately, even though the model loads later.
    const deadline = Date.now() + 90_000
    while (Date.now() < deadline) {
      if (!isProcessAlive(proc.pid)) break
      const health = await probeAirllm(options.port, 1500)
      if (health?.ok) {
        return {
          pid: proc.pid,
          port: options.port,
          model: options.modelName,
          detail: options.modelRepo,
        }
      }
      await new Promise((resolve) => setTimeout(resolve, 400))
    }

    await killProcess(proc.pid)
    throw new Error(
      "The deep engine did not start. Check ~/.imurun/logs/airllm.log - `imurun setup --deep` installs " +
        "airllm and torch if they are missing.",
    )
  }

  async stop(handle: Pick<EngineHandle, "pid" | "port">): Promise<void> {
    // Ask it to release the weights first, so a stop is a clean unload rather
    // than a kill that leaves a half-written cache behind.
    try {
      await fetch(`http://127.0.0.1:${handle.port}/unload`, {
        method: "POST",
        signal: AbortSignal.timeout(3000),
      })
    } catch {
      // not listening any more
    }
    if (handle.pid) await killProcess(handle.pid)
  }

  async health(port: number): Promise<EngineHealth> {
    const status = await probeAirllm(port)
    if (!status) return { healthy: false, detail: "not listening" }
    if (status.error) return { healthy: false, detail: status.error }
    const stage = status.stage ?? "idle"
    const percent = typeof status.percent === "number" ? ` ${status.percent}%` : ""
    const label =
      stage === "ready" ? "ready" : stage === "loading" ? `loading${percent}` : stage
    return {
      healthy: status.ok,
      loadedModel: status.model,
      detail: status.detail ? `${label} - ${status.detail}` : label,
    }
  }

  async contextSize(port: number): Promise<number | undefined> {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/props`, {
        signal: AbortSignal.timeout(3000),
      })
      if (!response.ok) return undefined
      const body = (await response.json()) as {
        default_generation_settings?: { n_ctx?: number }
        n_ctx?: number
      }
      const size = body.default_generation_settings?.n_ctx ?? body.n_ctx
      return typeof size === "number" && size > 0 ? size : undefined
    } catch {
      return undefined
    }
  }
}
