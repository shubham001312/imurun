/**
 * The deep engine's command line.
 *
 * These hold the line on the bug that broke every deep-model turn: the spawn
 * used to start with the *script* instead of the interpreter, and uv_spawn
 * answered EFTYPE ("inappropriate file type or format") on every machine.
 */
import { describe, expect, test } from "bun:test"
import { buildAirllmArgs, findPython } from "../src/engines/airllm"

const python = { command: "C:/venv/Scripts/python.exe", prefix: [] }
const server = "C:/imurun/engines/airllm/airllm_server.py"

describe("buildAirllmArgs", () => {
  test("the interpreter is the program, the script the first argument", () => {
    const args = buildAirllmArgs(python, server, { modelRepo: "Qwen/Qwen3-32B", port: 42425 })
    expect(args[0]).toBe("C:/venv/Scripts/python.exe")
    expect(args[1]).toBe(server)
    expect(args.slice(2)).toContain("--model")
  })

  test("the launcher prefix sits between the interpreter and the script", () => {
    const args = buildAirllmArgs({ command: "C:/Windows/py.exe", prefix: ["-3"] }, server, {
      modelRepo: "A/B",
      port: 1,
    })
    expect(args[0]).toBe("C:/Windows/py.exe")
    expect(args[1]).toBe("-3")
    expect(args[2]).toBe(server)
  })

  test("the repo, port and compression all arrive", () => {
    const args = buildAirllmArgs(python, server, {
      modelRepo: "deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct",
      modelName: "DeepLite",
      port: 42425,
      compression: "8bit",
      maxNewTokens: 2048,
    })
    expect(args).toContain("deepseek-ai/DeepSeek-Coder-V2-Lite-Instruct")
    expect(args).toContain("DeepLite")
    expect(args[args.indexOf("--port") + 1]).toBe("42425")
    expect(args[args.indexOf("--compression") + 1]).toBe("8bit")
    expect(args[args.indexOf("--max-new-tokens") + 1]).toBe("2048")
  })

  test("defaults hold when only a repo is given", () => {
    const args = buildAirllmArgs(python, server, { modelRepo: "A/B", port: 2 })
    expect(args[args.indexOf("--compression") + 1]).toBe("4bit")
    expect(args[args.indexOf("--max-new-tokens") + 1]).toBe("1024")
    // The display name falls out of the repo id.
    expect(args[args.indexOf("--name") + 1]).toBe("B")
  })
})

describe("findPython", () => {
  test("an explicit path wins without probing", () => {
    // The engine trusts the configured interpreter when it exists.
    expect(findPython(process.execPath)?.command).toBe(process.execPath)
  })
})
