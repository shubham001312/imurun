/**
 * Local-model session helpers — connect a session to the local runtime.
 *
 * Given a model reference (or nothing at all), these helpers decide which
 * installed model to use, start the runtime with it when needed, and keep the
 * client heartbeat alive while a session runs so the supervisor frees RAM as
 * soon as the client is gone.
 */
import type { ImurunConfig, InstalledModel } from "@imurun/schema"
import { ensureLlamaCppEngine, listInstalled, updateInstalled } from "@imurun/modelmgr"
import { logger } from "@imurun/util"
import { RuntimeManager } from "./manager"

export interface LocalTarget {
  /** Name handed to llama-server as --alias and used in the API ref. */
  modelName: string
  /** GGUF path. */
  path: string
  mmprojPath?: string
  /** Registry id (model-quant) when this target came from the store. */
  installedId?: string
  /** The model's trained context window — the origin for YaRN extension. */
  contextLength?: number
  /** Which engine should serve it. */
  engine?: "llama-cpp" | "airllm"
  /** The original weights, for the deep engine. */
  repo?: string
}

export interface LocalResolution {
  /** Set when the request should go through the local runtime. */
  target?: LocalTarget
  /** Model reference for the provider registry ("local/<name>" or the original). */
  modelRef?: string
  /** Set when an explicit local request cannot be fulfilled. */
  error?: string
}

/** Most recently used installed model, else the newest install. */
export function pickDefaultInstalled(installed: InstalledModel[]): InstalledModel | undefined {
  if (installed.length === 0) return undefined
  const used = installed.filter((m) => m.lastUsedAt !== undefined)
  if (used.length > 0) {
    return [...used].sort((a, b) => (b.lastUsedAt ?? 0) - (a.lastUsedAt ?? 0))[0]
  }
  return [...installed].sort((a, b) => b.installedAt - a.installedAt)[0]
}

export function findInstalled(installed: InstalledModel[], ref: string): InstalledModel | undefined {
  const lower = ref.toLowerCase()
  return (
    installed.find((m) => m.id.toLowerCase() === lower) ??
    installed.find((m) => m.modelId.toLowerCase() === lower) ??
    installed.find((m) => `${m.modelId}-${m.quant}`.toLowerCase() === lower) ??
    installed.find((m) => m.modelId.toLowerCase().includes(lower))
  )
}

function installedHint(installed: InstalledModel[]): string {
  if (installed.length === 0) return "No local models are installed yet — run `imurun models install qwen2.5-coder-7b`."
  return `Installed models: ${installed.map((m) => m.id).join(", ")}`
}

/**
 * Interprets a model reference for local execution.
 *
 *   undefined                    → the default installed model (if any)
 *   "qwen2.5-coder-7b"           → that installed model
 *   "qwen2.5-coder-7b-Q4_K_M"    → exact registry id
 *   "local/qwen2.5-coder-7b"     → explicit local
 *   "C:\\models\\x.gguf"          → raw file outside the store
 *   "openrouter/qwen/qwen3-32b"  → not local; passed through untouched
 */
export function resolveLocalTarget(ref: string | undefined, installed: InstalledModel[]): LocalResolution {
  if (!ref) {
    const entry = pickDefaultInstalled(installed)
    if (!entry) return {}
    return { target: targetOf(entry), modelRef: `local/${entry.modelId}` }
  }

  // `airllm/<repo>`: the deep engine, for weights that are never a GGUF.
  // The repo id keeps its slash, which is why the prefix is stripped rather
  // than split: `airllm/Qwen/Qwen2.5-Coder-32B-Instruct` is one repo.
  if (ref.toLowerCase().startsWith("airllm/")) {
    const repo = ref.slice("airllm/".length)
    const name = repo.replace(/\\/g, "/").replace(/\/$/, "").split("/").pop() ?? repo
    return {
      target: { modelName: name, path: "", engine: "airllm", repo },
      modelRef: repo,
    }
  }

  if (ref.toLowerCase().endsWith(".gguf")) {
    // The path may have been pasted from another machine, so both separators
    // are honoured when taking the model name out of it.
    const name = ref.replace(/\\/g, "/").split("/").pop() ?? ref
    const modelName = name.replace(/\.gguf$/i, "")
    return { target: { modelName, path: ref }, modelRef: `local/${modelName}` }
  }

  if (ref.startsWith("local/")) {
    const name = ref.slice("local/".length)
    const entry = findInstalled(installed, name)
    if (!entry) {
      return { error: `No installed model matches "${name}". ${installedHint(installed)}` }
    }
    return { target: targetOf(entry), modelRef: `local/${entry.modelId}` }
  }

  // a bare name that matches an installed model is treated as local
  const entry = findInstalled(installed, ref)
  if (entry) return { target: targetOf(entry), modelRef: `local/${entry.modelId}` }

  // A Hugging Face repo (`owner/repo`) is the deep engine — original weights,
  // streamed from disk. There is no prefix to remember: anything shaped like
  // a repo is one, and the engine is chosen for you. Legacy `airllm/` refs
  // are normalised to this shape above.
  if (looksLikeRepo(ref)) {
    const repo = ref.replace(/\\/g, "/")
    const name = repo.replace(/\/$/, "").split("/").pop() ?? repo
    return {
      target: { modelName: name, path: "", engine: "airllm", repo },
      modelRef: repo,
    }
  }

  return { modelRef: ref }
}

/**
 * True when a reference is shaped like a Hugging Face repo id — exactly one
 * slash, identifier-like on both sides, and not a file path or an installed
 * model (those resolve earlier).
 *
 * This is what makes the deep engine invisible: `imurun run --model
 * Qwen/Qwen3-32B` simply works, with IMURUN choosing the engine — no
 * `airllm/` prefix for anyone to learn.
 */
export function looksLikeRepo(ref: string): boolean {
  const trimmed = ref.trim().replace(/\\/g, "/")
  if (!trimmed || /\s/.test(trimmed)) return false
  // Namespaced app refs are decided elsewhere (cloud providers, local models).
  if (trimmed.startsWith("local/") || trimmed.toLowerCase().startsWith("airllm/")) return false
  // A Windows drive path (`C:/models/x`) is a file, not a repo.
  if (/^[a-zA-Z]:\//.test(trimmed)) return false
  // Weights files, whatever the separator.
  if (/\.(gguf|safetensors|bin)$/i.test(trimmed)) return false
  const slash = trimmed.indexOf("/")
  if (slash <= 0 || slash !== trimmed.lastIndexOf("/")) return false
  const owner = trimmed.slice(0, slash)
  const name = trimmed.slice(slash + 1)
  if (!owner || !name) return false
  // Hugging Face owners are letters, digits, dots and dashes; repo names add
  // underscores. Anything else is a path or a typo, not a repo.
  return /^[\w][\w.-]*$/.test(owner) && /^[\w][\w.~-]*$/.test(name)
}

function targetOf(entry: InstalledModel): LocalTarget {
  return {
    modelName: entry.modelId,
    path: entry.path,
    mmprojPath: entry.mmprojPath,
    installedId: entry.id,
    contextLength: entry.contextLength,
    // A GGUF file is llama.cpp's; the deep engine takes original weights.
    engine: "llama-cpp",
  }
}

export interface LocalSession {
  manager: RuntimeManager
  /** True when this call started (or restarted) the engine itself. */
  started: boolean
  /** Call in a finally block — lets the supervisor stop the engine. */
  detach: () => Promise<void>
}

/**
 * Ensures the runtime is serving `target`, then registers a heartbeat for the
 * session. Restarts the runtime when it is serving a different model.
 */
export async function ensureLocalRuntime(
  config: ImurunConfig,
  target: LocalTarget,
  sessionId: string,
  options: { unrefHeartbeat?: boolean } = {},
): Promise<LocalSession> {
  const manager = new RuntimeManager(config)

  // Register the client heartbeat before anything else. If we spawned the
  // supervisor first, it could tick before our heartbeat existed and — for a
  // fast-failing start — conclude no client ever needed the model.
  const detach = await manager.attach(sessionId, { unref: options.unrefHeartbeat })

  try {
    const status = await manager.status()
    let started = true
    if (status.running && status.state) {
      const serving = status.state.model
      if (serving !== target.modelName && serving !== target.installedId) {
        logger.info(`runtime is serving "${serving}", switching to ${target.modelName}`)
        await manager.stop()
      } else {
        // already up and serving this model — don't take ownership of it
        started = false
      }
    }

    try {
      await manager.start({
        modelPath: target.path || undefined,
        modelName: target.modelName,
        mmprojPath: target.mmprojPath,
        modelRepo: target.repo,
        engineId: target.engine,
        nativeContextSize: target.contextLength,
      })
    } catch (error) {
      // The deep engine has its own failure modes (no Python, no airllm, a
      // gated repo), and reinstalling llama.cpp would not help; pass it on.
      if (target.engine === "airllm") throw error
      // A prebuilt GPU build can fail to load on an unknown machine (missing
      // driver, unsupported runtime). Re-install the portable CPU build once and
      // retry — the user never has to choose an engine by hand.
      const message = error instanceof Error ? error.message : String(error)
      logger.warn(`llama.cpp failed to start (${message}) — reinstalling a portable CPU build and retrying`)
      await manager.stop().catch(() => {})
      const engine = await ensureLlamaCppEngine({ force: true, preferVariant: "cpu", onStatus: (m) => logger.info(m) })
      logger.info(`reinstalled ${engine.version ?? "llama.cpp"} (${engine.variant ?? "cpu"}) at ${engine.binaryPath}`)
      await manager.start({
        modelPath: target.path || undefined,
        modelName: target.modelName,
        mmprojPath: target.mmprojPath,
        modelRepo: target.repo,
        engineId: target.engine,
        nativeContextSize: target.contextLength,
      })
    }

    await manager.spawnSupervisor()
    await manager.activity(sessionId)

    // record usage so this becomes the default model next time
    if (target.installedId) {
      try {
        const installed = await listInstalled()
        if (installed.some((m) => m.id === target.installedId)) {
          await updateInstalled(target.modelName, { lastUsedAt: Date.now() })
        }
      } catch {
        // non-fatal
      }
    }

    return { manager, started, detach }
  } catch (error) {
    await detach().catch(() => {})
    throw error
  }
}
