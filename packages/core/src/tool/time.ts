/**
 * The `time` tool — the machine's clock, not the model's guess.
 *
 * A model's sense of "now" ends at its training cutoff, so every question that
 * contains "today", "this week", "how long ago" or a deadline is a chance to
 * answer confidently about the wrong year. The system prompt already states the
 * date at the start of a run; this tool is for the exact time, another zone,
 * or a duration, without spending a request to find out.
 *
 * It reads the clock only. Nothing here is stored, and nothing leaves the
 * machine.
 */
import { fail, ok, optionalString } from "./types"
import type { ImurunTool } from "./types"

const ISO_IN_ZONE: Intl.DateTimeFormatOptions = {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
}

function zoneName(now: Date, timeZone?: string): string {
  try {
    return (
      new Intl.DateTimeFormat("en-US", { timeZone, timeZoneName: "longOffset" })
        .formatToParts(now)
        .find((part) => part.type === "timeZoneName")?.value ?? ""
    )
  } catch {
    return ""
  }
}

/**
 * Renders a moment three ways: what a person would say, a sortable timestamp,
 * and the Unix second. The three together are unambiguous - a local time with
 * no zone is the classic source of "that was yesterday" mistakes.
 */
function describe(now: Date, timeZone?: string): string {
  const zone = timeZone ?? Intl.DateTimeFormat().resolvedOptions().timeZone ?? "local"
  const readable = new Intl.DateTimeFormat("en-GB", {
    dateStyle: "full",
    timeStyle: "long",
    ...(timeZone ? { timeZone } : {}),
  }).format(now)
  // sv-SE is the locale whose short date is already ISO-8601.
  const iso = new Intl.DateTimeFormat("sv-SE", { ...ISO_IN_ZONE, ...(timeZone ? { timeZone } : {}) })
    .format(now)
    .replace(" ", "T")
  const offset = zoneName(now, timeZone)
  return [
    `Now: ${readable}`,
    `ISO: ${iso}${offset ? ` ${offset}` : ""}`,
    `Zone: ${zone}`,
    `Unix: ${Math.floor(now.getTime() / 1000)}`,
  ].join("\n")
}

export const timeTool: ImurunTool = {
  name: "time",
  description:
    "The current date and time on this machine. Use it whenever the answer depends on 'now', a deadline, " +
    "how long ago something happened, or a date in a time zone other than the local one. Pass every zone you " +
    "need in `zones` — each zone's time comes straight from the tz database, so never convert between zones " +
    "by arithmetic. Never guess the date or the year.",
  parameters: {
    type: "object",
    properties: {
      timeZone: {
        type: "string",
        description: "Optional IANA time zone, for example Asia/Kolkata or America/New_York.",
      },
      zones: {
        type: "array",
        items: { type: "string" },
        description:
          "Several IANA time zones, answered in one call — for a world clock or a meeting across cities. " +
          "Each zone's time is read independently; no offsets are computed.",
      },
    },
    additionalProperties: false,
  },
  title(args) {
    const zones = Array.isArray(args.zones)
      ? args.zones.filter((zone): zone is string => typeof zone === "string")
      : []
    const one = typeof args.timeZone === "string" ? args.timeZone : ""
    const named = zones.length > 0 ? zones.join(", ") : one
    return named ? `time ${named}` : "time"
  },
  async execute(args) {
    const requested = optionalString(args, "timeZone")?.trim()
    const zones = Array.isArray(args.zones)
      ? args.zones.filter((zone): zone is string => typeof zone === "string" && zone.trim().length > 0)
      : []

    const now = new Date()
    if (zones.length > 0) {
      const parts: string[] = []
      for (const zone of zones) {
        try {
          parts.push(describe(now, zone))
        } catch {
          parts.push(
            `${zone}: unknown time zone. Use an IANA name such as Asia/Kolkata; the other zones below are still valid.`,
          )
        }
      }
      return ok(parts.join("\n\n"))
    }

    if (!requested) return ok(describe(now))
    try {
      return ok(describe(now, requested))
    } catch {
      // An invalid zone is a bad argument, not a failed command: say what was
      // wrong and let the model retry with a valid name.
      return fail(`Unknown time zone: ${requested}. Use an IANA name such as Asia/Kolkata.`)
    }
  },
}
