/**
 * grep — recursive content search with regex, include patterns, and context.
 * Skips binary files, ignored directories, and oversized files.
 */
import { readdir, readFile, stat } from "node:fs/promises"
import { basename, join, relative } from "node:path"
import type { ImurunTool } from "./types"
import { fail, ok, optionalBoolean, optionalNumber, optionalString, requireString } from "./types"
import { resolveToolPath } from "./read"
import { isIgnoredPath } from "./glob"

const DEFAULT_LIMIT = 100
const MAX_LIMIT = 1000
const MAX_FILE_BYTES = 1024 * 1024

interface Match {
  file: string
  line: number
  text: string
}

async function walkFiles(dir: string, out: string[], depth = 0): Promise<void> {
  if (depth > 24) return
  let entries
  try {
    entries = await readdir(dir, { withFileTypes: true })
  } catch {
    return
  }
  for (const entry of entries) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (isIgnoredPath(entry.name)) continue
      await walkFiles(full, out, depth + 1)
    } else if (entry.isFile()) {
      out.push(full)
    }
  }
}

function looksBinary(buffer: Buffer): boolean {
  return buffer.subarray(0, 8192).includes(0)
}

export const grepTool: ImurunTool = {
  name: "grep",
  description:
    "Search file contents for a regular expression. Returns matching lines with file paths and line numbers. " +
    "Use include to filter by extension (e.g. \"*.ts\") and path to narrow the search directory.",
  parameters: {
    type: "object",
    properties: {
      pattern: { type: "string", description: "Regular expression to search for." },
      path: { type: "string", description: "Directory or file to search (default: project directory)." },
      include: { type: "string", description: "Glob to filter files, e.g. \"*.ts\" or \"**/*.py\"." },
      caseSensitive: { type: "boolean", description: "Match case-sensitively (default true)." },
      limit: { type: "number", description: `Maximum matches to return (default ${DEFAULT_LIMIT}).` },
    },
    required: ["pattern"],
    additionalProperties: false,
  },
  title: (args) => `grep ${String(args.pattern ?? "")}`,
  async execute(args, ctx) {
    const patternText = requireString(args, "pattern")
    const searchPath = resolveToolPath(ctx.workingDir, optionalString(args, "path") ?? ".")
    const include = optionalString(args, "include")
    const caseSensitive = optionalBoolean(args, "caseSensitive") ?? true
    const limit = Math.min(optionalNumber(args, "limit") ?? DEFAULT_LIMIT, MAX_LIMIT)

    let regex: RegExp
    try {
      regex = new RegExp(patternText, caseSensitive ? "" : "i")
    } catch (error) {
      return fail(`Invalid regular expression: ${(error as Error).message}`)
    }

    const includeGlob = include ? new Bun.Glob(include) : undefined
    // A bare `*.ts` is a file-name pattern, not a whole-path one. Bun's glob
    // only matches it against a basename, so a file in `src/` would otherwise
    // be filtered out even though the docs promise `*.ts` finds it.
    const includeWholePath = include ? /[\\/]/.test(include) : false

    try {
      const target = await stat(searchPath).catch(() => undefined)
      if (!target) return fail(`Path not found: ${searchPath}`)

      const files: string[] = []
      if (target.isFile()) {
        files.push(searchPath)
      } else {
        await walkFiles(searchPath, files)
      }

      const matches: Match[] = []
      let scanned = 0

      for (const file of files) {
        if (matches.length >= limit) break
        const rel = relative(ctx.workingDir, file).replace(/\\/g, "/")
        if (isIgnoredPath(rel)) continue
        if (includeGlob) {
          const matchesInclude = includeWholePath
            ? includeGlob.match(rel) || includeGlob.match(file.replace(/\\/g, "/"))
            : includeGlob.match(basename(rel))
          if (!matchesInclude) continue
        }

        let info
        try {
          info = await stat(file)
        } catch {
          continue
        }
        if (info.size > MAX_FILE_BYTES) continue

        let buffer: Buffer
        try {
          buffer = await readFile(file)
        } catch {
          continue
        }
        if (looksBinary(buffer)) continue
        scanned++

        const lines = buffer.toString("utf8").split(/\r?\n/)
        for (let i = 0; i < lines.length; i++) {
          const line = lines[i]!
          if (regex.test(line)) {
            matches.push({
              file: rel,
              line: i + 1,
              text: line.length > 300 ? line.slice(0, 300) + "…" : line,
            })
            if (matches.length >= limit) break
          }
        }
      }

      if (matches.length === 0) {
        return ok(`No matches for /${patternText}/ in ${relative(ctx.workingDir, searchPath) || "."} (scanned ${scanned} files)`)
      }

      const grouped = new Map<string, Match[]>()
      for (const m of matches) {
        const list = grouped.get(m.file) ?? []
        list.push(m)
        grouped.set(m.file, list)
      }

      const rendered: string[] = []
      for (const [file, fileMatches] of grouped) {
        rendered.push(`${file}:`)
        for (const m of fileMatches) {
          rendered.push(`  ${m.line}\u2502 ${m.text}`)
        }
      }

      return ok(rendered.join("\n"), { title: `${matches.length} matches for /${patternText}/` })
    } catch (error) {
      return fail(`Grep failed: ${(error as Error).message}`)
    }
  },
}
