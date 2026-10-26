import { describe, expect, test } from "bun:test"
import type { InstalledModel } from "@imurun/schema"
import { findInstalled, looksLikeRepo, pickDefaultInstalled, resolveLocalTarget } from "../src"

function entry(partial: Partial<InstalledModel> & { modelId: string; quant: InstalledModel["quant"] }): InstalledModel {
  return {
    id: `${partial.modelId}-${partial.quant}`,
    path: `C:/models/${partial.modelId}.gguf`,
    sizeBytes: 1024,
    installedAt: 1000,
    ...partial,
  }
}

const entries: InstalledModel[] = [
  entry({ modelId: "qwen2.5-coder-7b", quant: "Q4_K_M", lastUsedAt: 5000 }),
  entry({ modelId: "qwen3-4b", quant: "Q5_K_M", installedAt: 9000 }),
]

describe("local model resolution", () => {
  test("default picks the most recently used model", () => {
    expect(pickDefaultInstalled(entries)?.modelId).toBe("qwen2.5-coder-7b")
  })

  test("default falls back to the newest install when none was used", () => {
    const fresh = [entry({ modelId: "a", quant: "Q4_K_M", installedAt: 1 }), entry({ modelId: "b", quant: "Q4_K_M", installedAt: 2 })]
    expect(pickDefaultInstalled(fresh)?.modelId).toBe("b")
  })

  test("no ref and nothing installed resolves to nothing (no error)", () => {
    expect(resolveLocalTarget(undefined, [])).toEqual({})
  })

  test("no ref uses the default installed model", () => {
    const resolution = resolveLocalTarget(undefined, entries)
    expect(resolution.modelRef).toBe("local/qwen2.5-coder-7b")
    expect(resolution.target?.path).toBe("C:/models/qwen2.5-coder-7b.gguf")
    expect(resolution.error).toBeUndefined()
  })

  test("bare catalog name matches an installed model", () => {
    const resolution = resolveLocalTarget("qwen3-4b", entries)
    expect(resolution.modelRef).toBe("local/qwen3-4b")
    expect(resolution.target?.mmprojPath).toBeUndefined()
  })

  test("exact registry id matches", () => {
    expect(resolveLocalTarget("qwen3-4b-Q5_K_M", entries).target?.installedId).toBe("qwen3-4b-Q5_K_M")
  })

  test("explicit local/ prefix requires the model to exist", () => {
    expect(resolveLocalTarget("local/qwen3-4b", entries).target).toBeDefined()
    const missing = resolveLocalTarget("local/nothing-here", entries)
    expect(missing.error).toContain("No installed model matches")
    expect(missing.error).toContain("qwen3-4b")
  })

  test("a .gguf path runs directly without the registry", () => {
    const resolution = resolveLocalTarget("D:\\models\\my-model.gguf", entries)
    expect(resolution.modelRef).toBe("local/my-model")
    expect(resolution.target?.path).toBe("D:\\models\\my-model.gguf")
    expect(resolution.target?.installedId).toBeUndefined()
  })

  test("cloud references pass through untouched", () => {
    const resolution = resolveLocalTarget("openrouter/qwen/qwen3-32b", entries)
    expect(resolution.target).toBeUndefined()
    expect(resolution.modelRef).toBe("openrouter/qwen/qwen3-32b")
    expect(resolution.error).toBeUndefined()
  })

  test("findInstalled supports partial names but not ambiguity across providers", () => {
    expect(findInstalled(entries, "coder")?.modelId).toBe("qwen2.5-coder-7b")
    expect(findInstalled(entries, "qwen")).toBeDefined() // first match wins
    expect(findInstalled(entries, "llama")).toBeUndefined()
  })

  test("a bare repo id is the deep engine — no prefix to learn", () => {
    const resolution = resolveLocalTarget("Qwen/Qwen2.5-Coder-32B-Instruct", entries)
    expect(resolution.modelRef).toBe("Qwen/Qwen2.5-Coder-32B-Instruct")
    expect(resolution.target?.engine).toBe("airllm")
    expect(resolution.target?.repo).toBe("Qwen/Qwen2.5-Coder-32B-Instruct")
    expect(resolution.target?.modelName).toBe("Qwen2.5-Coder-32B-Instruct")
    expect(resolution.error).toBeUndefined()
  })

  test("the legacy airllm/<repo> prefix resolves to the same thing, stored bare", () => {
    const resolution = resolveLocalTarget("airllm/Qwen/Qwen2.5-Coder-32B-Instruct", entries)
    expect(resolution.modelRef).toBe("Qwen/Qwen2.5-Coder-32B-Instruct")
    expect(resolution.target?.engine).toBe("airllm")
    expect(resolution.target?.repo).toBe("Qwen/Qwen2.5-Coder-32B-Instruct")
    expect(resolution.target?.modelName).toBe("Qwen2.5-Coder-32B-Instruct")
    expect(resolution.error).toBeUndefined()
  })

  test("repo shape is strict: paths, files and app refs are not repos", () => {
    for (const not of [
      "C:/models/weights",
      "models/weights.gguf",
      "local/qwen2.5-coder-7b",
      "airllm/Qwen/Qwen3-32B",
      "openrouter/minimax/minimax-m3.1-flash:free",
      "one/two/three",
      "/leading",
      "trailing/",
      "has space/x",
      "just-a-name",
    ]) {
      expect(looksLikeRepo(not)).toBe(false)
    }
    for (const repo of [
      "Qwen/Qwen3-32B",
      "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
      "meta-llama/Llama-3.2-1B",
      "org.name/model_name-v2",
    ]) {
      expect(looksLikeRepo(repo)).toBe(true)
    }
  })

  test("an installed GGUF always goes to llama.cpp, never the deep engine", () => {
    expect(resolveLocalTarget(undefined, entries).target?.engine).toBe("llama-cpp")
  })

  test("a local directory can be run deeply", () => {
    const resolution = resolveLocalTarget("airllm/D:\\weights\\my-model", entries)
    expect(resolution.target?.repo).toBe("D:\\weights\\my-model")
    expect(resolution.target?.modelName).toBe("my-model")
  })
})
