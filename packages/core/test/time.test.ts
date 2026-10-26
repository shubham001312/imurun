import { describe, expect, test } from "bun:test"
import { logger } from "@imurun/util"
import { buildSystemPrompt, nowLine } from "../src/session/system-prompt"
import { timeTool } from "../src/tool/time"
import { ToolRegistry } from "../src/tool/registry"

const ctx = {
  workingDir: process.cwd(),
  sessionId: "ses_test",
  log: logger.child("test"),
}

describe("the clock in the prompt", () => {
  test("is a local date and time with a real offset, not UTC midnight", () => {
    const line = nowLine(new Date("2026-10-21T18:30:00Z"))
    expect(line).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2} \(.+, UTC[+-]\d{2}:\d{2}\)$/)
    // The date is rendered in local time, so it is the same calendar day the
    // user is in, not whatever UTC says.
    expect(line).toStartWith(new Date("2026-10-21T18:30:00Z").toLocaleDateString("en-CA"))
  })

  test("the prompt names it, and tells the model not to guess dates", () => {
    const prompt = buildSystemPrompt({ workingDir: "C:/work" })
    expect(prompt).toContain("- Now: ")
    expect(prompt).not.toContain("- Today: ")
    expect(prompt).toContain("Never guess the date")
  })
})

describe("the time tool", () => {
  test("is registered by default and never needs approval", async () => {
    expect(ToolRegistry.default().get("time")).toBeDefined()
    const { AUTO_ALLOW_TOOLS } = await import("@imurun/schema")
    expect(AUTO_ALLOW_TOOLS).toContain("time")
  })

  test("reports the local time, an ISO timestamp and the Unix second", async () => {
    const result = await timeTool.execute({}, ctx)
    expect(result.ok).toBe(true)
    expect(result.output).toContain("ISO: ")
    expect(result.output).toMatch(/Unix: \d{10,}/)
    expect(result.output).toContain("Zone: ")
  })

  test("answers in another zone when asked", async () => {
    const result = await timeTool.execute({ timeZone: "Asia/Kolkata" }, ctx)
    expect(result.ok).toBe(true)
    expect(result.output).toContain("Zone: Asia/Kolkata")
    expect(result.output).toContain("+05:30")
  })

  test("answers several zones in one call, each from the tz database", async () => {
    const result = await timeTool.execute(
      { zones: ["Asia/Kolkata", "America/New_York", "Asia/Tokyo"] },
      ctx,
    )
    expect(result.ok).toBe(true)
    expect(result.output).toContain("Zone: Asia/Kolkata")
    expect(result.output).toContain("Zone: America/New_York")
    expect(result.output).toContain("Zone: Asia/Tokyo")
    // One moment in time, three honest local renderings.
    const stamps = [...result.output.matchAll(/Unix: (\d+)/g)].map((match) => match[1])
    expect(new Set(stamps).size).toBe(1)
  })

  test("an unknown zone in a list fails alone and names the others", async () => {
    const result = await timeTool.execute({ zones: ["Asia/Kolkata", "Mars/Olympus"] }, ctx)
    expect(result.ok).toBe(true)
    expect(result.output).toContain("Zone: Asia/Kolkata")
    expect(result.output).toContain("Mars/Olympus: unknown time zone")
  })

  test("an unknown zone is a clear failure, not a crash", async () => {
    const result = await timeTool.execute({ timeZone: "Mars/Olympus_Mons" }, ctx)
    expect(result.ok).toBe(false)
    expect(result.error).toContain("Unknown time zone")
    expect(result.error).toContain("Asia/Kolkata")
  })

  test("the title is readable in a permission prompt", () => {
    expect(timeTool.title!({})).toBe("time")
    expect(timeTool.title!({ timeZone: "UTC" })).toBe("time UTC")
  })
})
