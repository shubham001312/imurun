/**
 * Summarization — turns a conversation into a durable digest.
 *
 * Digests are deliberately *extractive and deterministic*: they run offline, in
 * constant time, with no model call, so a summary can be produced while the
 * user is still typing and never fails when the model is unavailable. The
 * result is what gets written to `~/.imurun/memory/sessions/<id>.md` and fed
 * back into the system prompt when old turns are compacted away.
 *
 * Anything the model would add on top is optional: the digest is already
 * complete enough to answer "what did we do here, and what is still open?".
 */
import type { Message } from "@imurun/schema"

export type DigestKind = "session_summary"

export interface SessionDigest {
  sessionId: string
  title: string
  workingDir: string
  model?: string
  createdAt: number
  updatedAt: number
  messageCount: number
  /** Paths touched through the file tools (deduped, capped). */
  files: string[]
  /** Commands executed through the shell tool (newest last, capped). */
  commands: string[]
  /** Unfinished work, carried forward until the user closes it. */
  todos: string[]
  /** Work that was explicitly completed. */
  done: string[]
  /** Notable assistant statements, one line each. */
  highlights: string[]
  /**
   * Durable facts stated in the conversation ("the API listens on :8080").
   * Optional so digests written by older versions still load.
   */
  facts?: string[]
  /** Names of keys or tokens mentioned. Never their values. */
  keys?: string[]
  /** The subjects the session was about, drawn from headings and identifiers. */
  topics?: string[]
  /**
   * How many earlier messages this digest stands in for because they were
   * removed from the session. When it is greater than zero the digest is the
   * only record of them, so it must be injected on every run - not just the
   * run that folded them.
   */
  folded?: number
  /** Last message id covered by this digest (compaction watermark). */
  upToMessageId?: string
  generatedAt: number
}

const MAX_FILES = 40
const MAX_COMMANDS = 20
const MAX_TODOS = 20
const MAX_DONE = 20
const MAX_HIGHLIGHTS = 12
const MAX_FACTS = 20
const MAX_KEYS = 12
const MAX_TOPICS = 12
const MAX_ITEM = 200

/**
 * `- [ ] fix the parser`, `TODO: wire the API`, `Next step: write tests`.
 *
 * The bracketed checkbox form stands on its own; the word forms require either
 * a colon or a `TODO`-style keyword, so ordinary prose that happens to start
 * with "Next …" is never mistaken for a task.
 */
const OPEN_TODO_PATTERNS: RegExp[] = [
  /^\s*[-*+]\s*\[ \]\s*(.+)$/i,
  /^\s*(?:TODO|FIXME|XXX)\s*:?\s+(.+)$/i,
  /^\s*(?:Next(?:\s+step)?|Remaining|Follow-?up|Still\s+to\s+do)\s*:\s*(.+)$/i,
]

/** `- [x] done thing`, `DONE: thing` — the word form must carry a colon. */
const DONE_PATTERNS: RegExp[] = [
  /^\s*[-*+]\s*\[[xX]\]\s*(.+)$/,
  /^\s*(?:DONE|COMPLETED|FINISHED)\s*:\s*(.+)$/i,
]

/**
 * Facts worth keeping when the turns themselves are folded away.
 *
 * Only explicit forms count. A sentence that merely looks like a statement is
 * not a fact, and pretending otherwise fills the summary with noise.
 */
const FACT_PATTERNS: RegExp[] = [
  /^\s*(?:NOTE|FACT|IMPORTANT|REMEMBER|CONSTRAINT)\s*:\s*(.+)$/i,
  /^\s*\*\*(?:note|fact|important|remember|constraint)\*\*\s*:?\s*(.+)$/i,
]

/** A credential's *name* is useful; its value must never be written down. */
const KEY_NAME = /\b([A-Z][A-Z0-9_]{2,}(?:_KEY|_TOKEN|_SECRET|_PASSWORD|_PASS|_PAT|_CREDENTIAL|_AUTH))\b/g

/** Values in `NAME=value` are dropped everywhere a fact is captured. */
function withoutSecrets(text: string): string {
  return text
    .replace(
      /\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASS|PAT|CREDENTIAL|AUTH))\s*[:=]\s*\S+/g,
      "$1=<redacted>",
    )
    .replace(/\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}/g, "<redacted>")
    .replace(/\bgh[pousr]_[A-Za-z0-9]{16,}/g, "<redacted>")
    .replace(/\bnpm_[A-Za-z0-9]{16,}/g, "<redacted>")
    .replace(/\b[A-Za-z0-9+/_-]{40,}={0,2}\b/g, "<redacted>")
}

function clamp(text: string, max = MAX_ITEM): string {
  const clean = text.replace(/\s+/g, " ").trim()
  if (clean.length <= max) return clean
  return `${clean.slice(0, max - 1)}\u2026`
}

function uniquePush(target: string[], value: string, max: number): void {
  // One choke point for everything that reaches the digest file: a command or
  // a highlight can contain an inline credential just as easily as a fact.
  const cleaned = clamp(withoutSecrets(value))
  if (!cleaned) return
  if (target.includes(cleaned)) return
  target.push(cleaned)
  while (target.length > max) target.shift()
}

/** Flattens any message content shape into plain text. */
export function messageText(message: Message): string {
  if (typeof message.content === "string") return message.content
  return message.content
    .map((part) => {
      if (part.type === "text") return part.text
      if (part.type === "file") return `[file: ${part.path}]`
      return "[image]"
    })
    .join("\n")
}

/** Pulls unfinished todos out of free text. */
export function extractTodos(text: string): string[] {
  const found: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    for (const pattern of OPEN_TODO_PATTERNS) {
      const match = pattern.exec(raw)
      if (match?.[1]) {
        const todo = clamp(match[1])
        if (todo && !found.includes(todo)) found.push(todo)
        break
      }
    }
  }
  return found
}

/** Pulls explicitly-finished work out of free text. */
export function extractDone(text: string): string[] {
  const found: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    for (const pattern of DONE_PATTERNS) {
      const match = pattern.exec(raw)
      if (match?.[1]) {
        const done = clamp(match[1])
        if (done && !found.includes(done)) found.push(done)
        break
      }
    }
  }
  return found
}

/** Pulls explicit facts out of free text, with credential values removed. */
export function extractFacts(text: string): string[] {
  const found: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    for (const pattern of FACT_PATTERNS) {
      const match = pattern.exec(raw)
      if (match?.[1]) {
        const fact = clamp(withoutSecrets(match[1]))
        if (fact && !found.includes(fact)) found.push(fact)
        break
      }
    }
  }
  return found
}

/**
 * The *names* of keys and tokens that were mentioned.
 *
 * Names are worth carrying forward ("NPM_TOKEN is set"), values never are, so
 * this matches identifiers only - the regex has no group that could capture a
 * value.
 */
export function extractKeyNames(text: string): string[] {
  const found: string[] = []
  for (const match of text.matchAll(KEY_NAME)) {
    const name = match[1]
    if (name && !found.includes(name)) found.push(name)
  }
  return found
}

/** The subjects of a conversation: headings, and the things in backticks. */
export function extractTopics(text: string): string[] {
  const found: string[] = []
  for (const raw of text.split(/\r?\n/)) {
    const heading = /^\s{0,3}#{1,4}\s+(.+)$/.exec(raw)
    if (heading?.[1]) uniquePush(found, heading[1], MAX_TOPICS)
  }
  for (const match of text.matchAll(/`([A-Za-z][\w.:/-]{2,40})`/g)) {
    const token = match[1]
    if (!token) continue
    // A single lowercase word is prose that happened to be in backticks.
    if (/^[a-z]+$/.test(token) && token.length < 8) continue
    uniquePush(found, token, MAX_TOPICS)
  }
  return found
}

/** Renders a digest as markdown — the human- and RAG-readable form on disk. */
export function digestToMarkdown(digest: SessionDigest): string {
  const lines: string[] = []
  lines.push(`# ${digest.title}`)
  lines.push("")
  lines.push(`- session: \`${digest.sessionId}\``)
  lines.push(`- project: \`${digest.workingDir}\``)
  if (digest.model) lines.push(`- model: \`${digest.model}\``)
  lines.push(`- messages: ${digest.messageCount}`)
  if ((digest.folded ?? 0) > 0) {
    lines.push(`- earlier messages folded into this summary: ${digest.folded}`)
  }
  lines.push(`- updated: ${new Date(digest.updatedAt).toISOString()}`)
  lines.push("")

  if (digest.todos.length > 0) {
    lines.push("## Open todos")
    for (const todo of digest.todos) lines.push(`- [ ] ${todo}`)
    lines.push("")
  }
  if (digest.done.length > 0) {
    lines.push("## Completed")
    for (const done of digest.done) lines.push(`- [x] ${done}`)
    lines.push("")
  }
  if (digest.files.length > 0) {
    lines.push("## Files touched")
    for (const file of digest.files) lines.push(`- \`${file}\``)
    lines.push("")
  }
  if (digest.commands.length > 0) {
    lines.push("## Commands")
    for (const command of digest.commands) lines.push(`- \`${command}\``)
    lines.push("")
  }
  if (digest.highlights.length > 0) {
    lines.push("## Highlights")
    for (const highlight of digest.highlights) lines.push(`- ${highlight}`)
    lines.push("")
  }
  if ((digest.facts?.length ?? 0) > 0) {
    lines.push("## Facts")
    for (const fact of digest.facts ?? []) lines.push(`- ${fact}`)
    lines.push("")
  }
  if ((digest.keys?.length ?? 0) > 0) {
    lines.push("## Key names mentioned")
    lines.push("")
    lines.push("_Names only. No value is ever written down._")
    lines.push("")
    for (const key of digest.keys ?? []) lines.push(`- \`${key}\``)
    lines.push("")
  }
  if ((digest.topics?.length ?? 0) > 0) {
    lines.push("## Topics")
    for (const topic of digest.topics ?? []) lines.push(`- ${topic}`)
    lines.push("")
  }
  return lines.join("\n")
}

/** Compact rendering injected into the system prompt after compaction. */
export function digestToPrompt(digest: SessionDigest): string {
  const lines: string[] = [`Session "${digest.title}" (${digest.messageCount} messages).`]
  if ((digest.folded ?? 0) > 0) {
    lines.push(
      `This summary stands in for ${digest.folded} earlier message(s) that were removed from the window; ` +
        "it is the record of what happened, so treat it as established context.",
    )
  }
  if (digest.done.length > 0) lines.push(`Completed: ${digest.done.slice(-6).join("; ")}.`)
  if (digest.todos.length > 0) lines.push(`Open todos: ${digest.todos.slice(0, 8).join("; ")}.`)
  if (digest.files.length > 0) lines.push(`Files touched: ${digest.files.slice(-12).join(", ")}.`)
  if (digest.commands.length > 0) lines.push(`Recent commands: ${digest.commands.slice(-5).join(" | ")}.`)
  if ((digest.facts?.length ?? 0) > 0) {
    lines.push(`Facts established: ${(digest.facts ?? []).slice(-10).join("; ")}`)
    // Facts age. Anything here that came from a web search was true when it
    // was found, not necessarily now — the thinking step judges each one
    // before reusing it, and the date below is how it knows how old it is.
    lines.push(
      `These facts were recorded on ${new Date(digest.updatedAt).toISOString().slice(0, 10)}; ` +
        "before relying on one for a time-sensitive answer, re-check it (search again rather than reuse).",
    )
  }
  if ((digest.keys?.length ?? 0) > 0) {
    lines.push(`Key names mentioned (never values): ${(digest.keys ?? []).slice(-8).join(", ")}.`)
  }
  if ((digest.topics?.length ?? 0) > 0) {
    lines.push(`Topics: ${(digest.topics ?? []).slice(-10).join(", ")}.`)
  }
  if (digest.highlights.length > 0) {
    lines.push("Earlier in this session:")
    for (const highlight of digest.highlights.slice(-8)) lines.push(`- ${highlight}`)
  }
  return lines.join("\n")
}

export interface BuildDigestInput {
  sessionId: string
  title: string
  workingDir: string
  model?: string
  createdAt?: number
  updatedAt?: number
  messages: Message[]
  /** Digest from the previous pass; its open todos carry forward. */
  previous?: SessionDigest
  /** Messages removed from the session by this pass, if it compacted. */
  folded?: number
}

/**
 * Builds a digest covering `messages`. Open todos from `previous` are carried
 * forward unless the new messages mark them done, so nothing silently falls
 * off the list between passes.
 */
export function buildDigest(input: BuildDigestInput): SessionDigest {
  // A summary of a summary must not lose the earlier record. Everything that
  // accumulates starts with what the previous pass established, then adds what
  // this pass found, so folding the same conversation twice keeps its history.
  const files: string[] = [...(input.previous?.files ?? [])]
  const commands: string[] = [...(input.previous?.commands ?? [])]
  const highlights: string[] = [...(input.previous?.highlights ?? [])]
  const todos: string[] = []
  const done: string[] = [...(input.previous?.done ?? [])]
  const facts: string[] = [...(input.previous?.facts ?? [])]
  const keys: string[] = [...(input.previous?.keys ?? [])]
  const topics: string[] = [...(input.previous?.topics ?? [])]

  for (const message of input.messages) {
    const text = messageText(message)

    // Facts, key names and topics are recorded wherever they appear: a fact
    // stated in an answer is as worth keeping as one stated in a question.
    for (const fact of extractFacts(text)) uniquePush(facts, fact, MAX_FACTS)
    for (const key of extractKeyNames(text)) uniquePush(keys, key, MAX_KEYS)
    for (const topic of extractTopics(text)) uniquePush(topics, topic, MAX_TOPICS)

    if (message.role === "user" && text.trim()) {
      for (const todo of extractTodos(text)) uniquePush(todos, todo, MAX_TODOS)
      for (const item of extractDone(text)) uniquePush(done, item, MAX_DONE)
    }

    if (message.role === "assistant") {
      for (const call of message.toolCalls ?? []) {
        const args = (typeof call.args === "object" && call.args !== null ? call.args : {}) as Record<string, unknown>
        for (const key of ["path", "file", "filePath", "dir"]) {
          if (typeof args[key] === "string") uniquePush(files, args[key], MAX_FILES)
        }
        if (call.name === "shell" && typeof args.command === "string") {
          uniquePush(commands, args.command, MAX_COMMANDS)
        }
      }
      const first = text
        .split(/\r?\n/)
        .map((line) => line.trim())
        .find((line) => line.length > 0)
      if (first && !first.startsWith("{") && !first.startsWith("- [")) uniquePush(highlights, first, MAX_HIGHLIGHTS)
      for (const todo of extractTodos(text)) uniquePush(todos, todo, MAX_TODOS)
      for (const item of extractDone(text)) uniquePush(done, item, MAX_DONE)
    }
  }

  // carry forward unresolved todos from the previous digest
  for (const todo of input.previous?.todos ?? []) {
    if (done.some((item) => item === todo)) continue
    uniquePush(todos, todo, MAX_TODOS)
  }

  const last = input.messages[input.messages.length - 1]
  const first = input.messages[0]

  return {
    sessionId: input.sessionId,
    title: input.title,
    workingDir: input.workingDir,
    model: input.model,
    createdAt: input.createdAt ?? first?.createdAt ?? Date.now(),
    updatedAt: input.updatedAt ?? last?.createdAt ?? Date.now(),
    messageCount: input.messages.length,
    files,
    commands,
    todos,
    done,
    highlights,
    facts,
    keys,
    topics,
    folded: (input.previous?.folded ?? 0) + (input.folded ?? 0),
    upToMessageId: last?.id,
    generatedAt: Date.now(),
  }
}
