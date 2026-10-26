/**
 * Transcript rendering — turns conversation blocks into styled terminal lines.
 *
 * Pure: given the same blocks and width it always produces the same lines, so
 * the framing logic can be unit tested without a terminal.
 */
import { theme, sym } from "./theme"
import chalk from "chalk"
import { clampVisible, padVisible, visibleWidth, wrapText } from "./text"
import { hyperlink } from "./hyperlink"
import { spinnerFrame } from "./animate"
import { DEFAULT_MARKDOWN_PALETTE, highlightCode, renderMarkdown, type MarkdownPalette } from "./markdown"
import { progressBar, progressFraction, progressPhrase } from "./progress"

/**
 * Turns bare URLs into OSC 8 links, after wrapping.
 *
 * Post-processing matters: wrapping measures visible characters, so it has to
 * see the plain text. Terminals without OSC 8 ignore the sequence and show the
 * URL itself, so nothing is lost either way.
 */
function linkify(line: string): string {
  return line.replace(/https?:\/\/[^\s<>()[\]"'`]+/g, (url) => hyperlink(url, url))
}

/**
 * The answer's own typography.
 *
 * Everything else in the transcript is quiet - dim tool lines, muted input,
 * a collapsed thought. The answer is the one thing the user came for, so it
 * gets the brand's warm palette rather than the default terminal rainbow:
 * sand for headings and code, the one red for keywords, grey for comments.
 * Built per render because `setTheme` swaps the theme's values in place.
 */
function answerPalette(): Partial<MarkdownPalette> {
  return {
    heading: theme.accent.bold,
    code: theme.accent,
    keyword: theme.red,
    string: theme.ok,
    number: theme.warn,
    comment: theme.dim,
    bullet: theme.accent,
    quote: theme.dim,
    rule: theme.rule,
    link: (text: string) => theme.path.underline(text),
    dim: theme.dim,
  }
}

export type Tone = "info" | "ok" | "warn" | "error"

/**
 * What kind of thinking a block holds.
 *
 * The colour and the icon say what the model is doing without opening the
 * block: a search is blue, a file scan is amber, plain reasoning is violet.
 */
export type ThinkingCategory = "reasoning" | "search" | "files" | "shell" | "plan"

export interface ThinkingStyle {
  icon: string
  color: (text: string) => string
  label: string
}

const THINKING_STYLES: Record<ThinkingCategory, ThinkingStyle> = {
  reasoning: { icon: "◈", color: (text) => chalk.hex("#A78BFA")(text), label: "thinking" },
  search: { icon: "⌕", color: (text) => chalk.hex("#60A5FA")(text), label: "searching" },
  files: { icon: "◉", color: (text) => chalk.hex("#E0A458")(text), label: "reading" },
  shell: { icon: "⌘", color: (text) => chalk.hex("#38BDF8")(text), label: "running" },
  plan: { icon: "▤", color: (text) => chalk.hex("#34D399")(text), label: "planning" },
}

/** The style for a category, falling back to plain reasoning. */
export function thinkingStyle(category: ThinkingCategory | undefined): ThinkingStyle {
  return THINKING_STYLES[category ?? "reasoning"] ?? THINKING_STYLES.reasoning!
}

/**
 * The category a tool call implies. The block is coloured by what the agent
 * is actually doing, not by what it is thinking about.
 */
export function categoryForTool(name: string): ThinkingCategory {
  if (name === "web_search" || name === "web_fetch" || name === "github") return "search"
  if (name === "read" || name === "glob" || name === "grep" || name === "code_search") return "files"
  if (name === "shell") return "shell"
  if (name === "plan" || name === "todo" || name === "progress") return "plan"
  return "reasoning"
}

export type Block =
  /** `state` marks a message that is queued ("pending") or taken back ("undone"). */
  | { kind: "user"; text: string; state?: "pending" | "undone" }
  | { kind: "assistant"; text: string; streaming?: boolean }
  /**
   * The model's thinking. Collapsed by default to one line ("thinking · 12s");
   * a click (or /thinking) expands it in place while it keeps streaming.
   */
  | {
      kind: "reasoning"
      text: string
      expanded?: boolean
      seconds?: number
      live?: boolean
      /** When the block started, so the timer can tick while it streams. */
      startedAt?: number
      /** What kind of thinking this is, which decides its colour and icon. */
      category?: ThinkingCategory
      /**
       * Overrides the "thinking · 12s" label. Used by blocks that share the
       * collapsed/click-to-expand behaviour but are not thinking - research,
       * for one.
       */
      label?: string
    }
  | {
      kind: "tool"
      title: string
      status: "running" | "ok" | "fail"
      detail?: string
      /** Set while running, so the phrase and bar can advance with wall time. */
      startedAt?: number
      words?: readonly string[]
      estimateMs?: number
    }
  /** A preview of code the agent just wrote, so the edit is visible in the chat. */
  | { kind: "code"; text: string; lang?: string; title?: string; note?: string; maxLines?: number }
  /** Right-aligned one-liner (turn stats) — keeps the left gutter for chat. */
  | { kind: "meta"; text: string }
  | { kind: "notice"; text: string; tone?: Tone }
  | { kind: "raw"; text: string }
  | { kind: "gap" }

// Looked up at render time rather than captured: `setTheme` swaps the colour
// functions on the shared `theme` object, and tones must follow the palette.
const TONE_COLOR: Record<Tone, (s: string) => string> = {
  info: (s) => theme.dim(s),
  ok: (s) => theme.ok(s),
  warn: (s) => theme.warn(s),
  error: (s) => theme.error(s),
}

const TONE_GLYPH: Record<Tone, string> = {
  info: sym.diamond,
  ok: sym.ok,
  warn: sym.warn,
  error: sym.fail,
}

/** A small, quiet mark for each tool family, so calls are scannable. */
const TOOL_ICON: Record<string, string> = {
  read: "\u25c9", // ◉
  glob: "\u2315", // ⌕
  grep: "\u2315",
  code_search: "\u2315",
  write: "\u270e", // ✎
  edit: "\u270e",
  shell: "\u2318", // ⌘
  web_search: "\u2315",
  web_fetch: "\u21e3", // ⇣
  task: "\u21b3", // ↳
  memory: "\u25c7", // ◇
  progress: "\u25c7",
  skill: "\u25c7",
}

/** The icon for a tool title such as "read src/auth.ts". */
export function toolIcon(title: string): string {
  const name = title.trim().split(/\s+/)[0] ?? ""
  return TOOL_ICON[name] ?? sym.diamond
}

/** Prefixes the first line and indents the rest, then clamps to `width`. */
function withPrefix(lines: string[], prefix: string, continuation: string, width: number): string[] {
  return lines.map((line, index) => clampVisible(`${index === 0 ? prefix : continuation}${line}`, width))
}

/** Options threaded through rendering (e.g. the streaming caret glyph). */
export interface RenderOptions {
  /** Caret appended to a streaming assistant message. */
  caret?: string
  /** Animation clock, so running tools can advance their phrase. */
  tick?: number
  /** Wall clock used for the same; injectable so rendering stays testable. */
  now?: number
}

/** Renders one block into styled lines no wider than `width`. */
export function renderBlock(block: Block, width: number, options: RenderOptions = {}): string[] {
  switch (block.kind) {
    case "gap":
      return [""]

    case "user": {
      const state = block.state
      const body = renderMarkdown(block.text, { width: Math.max(width - 2, 8) })
      // The chevron is the one red mark in the transcript: it belongs to the
      // person typing, not to the machine.
      const marker =
        state === "pending" ? theme.warn(sym.queued) : state === "undone" ? theme.dim(sym.undone) : theme.red(sym.arrow)
      const lines = withPrefix(body, `${marker} `, "  ", width)
      if (state === "pending") return lines.map((line) => theme.muted(line))
      if (state === "undone") return lines.map((line) => theme.dim(line))
      return lines.map((line) => theme.muted(line))
    }

    case "assistant": {
      const caret = block.streaming ? (options.caret ?? "\u258c") : ""
      if (!block.text.trim() && !caret) return []
      return renderMarkdown(block.text, { width, caret: caret || undefined, palette: answerPalette() })
    }

    case "reasoning": {
      const now = options.now ?? Date.now()
      // While the block is live the timer ticks; when it closes, the elapsed
      // time is frozen into the block. The seconds are padded to two digits
      // so the label does not change width as it counts.
      const style = thinkingStyle(block.category)
      const elapsed = block.live && block.startedAt !== undefined
        ? Math.max(Math.floor((now - block.startedAt) / 1000), 0)
        : (block.seconds ?? 0)
      const clock = `${String(elapsed).padStart(2, "0")}s`
      const label = block.label ?? `${style.label} \u00b7 ${clock}`
      const head =
        `  ${style.color(style.icon)} ` +
        theme.dim(block.expanded ? label : `${label} \u2014 click to expand`)
      if (!block.expanded) {
        // Collapsed means collapsed: the model's reasoning stays inside the
        // block until it is clicked open. Only the label is visible, so a fast
        // stream can never push half-thoughts through the transcript.
        return [clampVisible(head, width)]
      }
      const wrapped = wrapText(block.text, Math.max(width - 4, 8))
      const lines = [clampVisible(head, width)]
      for (const line of wrapped) lines.push(clampVisible(`    ${theme.dim(linkify(line))}`, width))
      return lines
    }

    case "tool": {
      const running = block.status === "running"
      const icon = running
        ? theme.warn(spinnerFrame(options.tick ?? 0))
        : theme.accent(toolIcon(block.title))

      const now = options.now ?? Date.now()
      const elapsed = running ? Math.max(now - (block.startedAt ?? now), 0) : 0
      const phrase =
        running && block.words && block.words.length > 0 ? progressPhrase(block.words, elapsed) : undefined

      // "read src/auth.ts" reads better as a name and a target: the verb stays
      // bold, the path takes the code color, and the outcome sits far right.
      const split = block.title.trim().split(/\s+/)
      const verb = split[0] ?? block.title
      const target = block.title.trim().slice(verb.length).trim()
      let head =
        `  ${icon} ${theme.bold(verb)}` +
        (target ? ` ${theme.path(target)}` : "") +
        (phrase ? `  ${theme.dim(sym.dot)} ${theme.accent(phrase)}` : "")

      if (!running) {
        const mark = block.status === "ok" ? theme.ok(sym.ok) : theme.error(sym.fail)
        if (visibleWidth(head) + 4 <= width) {
          // One clear cell before the sidebar divider: the outcome belongs to
          // the call, not to the panel.
          head += " ".repeat(Math.max(width - visibleWidth(head) - 2, 1)) + mark + " "
        }
      }
      const lines = [head]

      if (running && block.estimateMs) {
        const fraction = progressFraction(elapsed, block.estimateMs)
        const barWidth = Math.max(Math.min(width - 12, 24), 8)
        lines.push(
          `    ${theme.rule(sym.vbar)} ${theme.accent(progressBar(fraction, barWidth))} ${theme.dim(`${Math.round(fraction * 100)}%`)}`,
        )
      }
      if (block.detail) {
        const wrapped = wrapText(block.detail, Math.max(width - 7, 8))
        for (const line of wrapped.slice(0, 4)) {
          lines.push(`    ${theme.rule(sym.vbar)} ${theme.dim(line)}`)
        }
      }
      return lines.map((line) => clampVisible(line, width))
    }

    case "code": {
      const label = block.title ?? block.lang ?? ""
      const head = label ? ` ${label} ` : ""
      const dashes = Math.max(width - 3 - head.length, 0)
      const lines = [theme.red("\u256d\u2500") + theme.accent(head) + theme.rule(`${"\u2500".repeat(dashes)}\u256e`)]

      const body = block.text.replace(/\r\n?/g, "\n").replace(/\n$/, "").split("\n")
      const maxLines = block.maxLines ?? 32
      const shown = body.slice(0, maxLines)
      for (const line of shown) {
        lines.push(`${theme.rule("\u2502 ")}${highlightCode(line, block.lang ?? "", DEFAULT_MARKDOWN_PALETTE)}`)
      }
      if (shown.length === 0) lines.push(theme.rule("\u2502"))
      if (body.length > shown.length) lines.push(theme.rule(`\u2502 \u2026 ${body.length - shown.length} more line(s)`))
      if (block.note) lines.push(theme.rule(`\u2502 ${block.note}`))
      lines.push(theme.rule(`\u2570${"\u2500".repeat(Math.max(width - 2, 0))}\u256f`))
      return lines.map((line) => clampVisible(line, width))
    }

    case "meta": {
      const text = clampVisible(block.text, Math.max(width - 2, 8))
      const pad = Math.max(width - visibleWidth(text) - 2, 0)
      return [`${" ".repeat(pad)}${theme.dim(text)}`]
    }

    case "notice": {
      const tone = block.tone ?? "info"
      const wrapped = wrapText(block.text, Math.max(width - 4, 8))
      const first = `  ${TONE_COLOR[tone](TONE_GLYPH[tone])} ${TONE_COLOR[tone](wrapped[0] ?? "")}`
      const rest = wrapped.slice(1).map((line) => `    ${TONE_COLOR[tone](line)}`)
      return [first, ...rest]
    }

    case "raw":
      // pre-formatted, possibly multi-line and already styled (command output)
      return block.text.split(/\r?\n/).map((line) => clampVisible(line, width))
  }
}

/** Flattens blocks into the full transcript line list. */
export function renderTranscript(blocks: Block[], width: number, options: RenderOptions = {}): string[] {
  const lines: string[] = []
  for (const block of blocks) {
    const rendered = renderBlock(block, width, options)
    if (rendered.length === 0) continue
    lines.push(...rendered)
  }
  return lines
}

export interface BlockRange {
  /** Index into the blocks array. */
  index: number
  /** First transcript line the block occupies (0-based, inclusive). */
  start: number
  /** Last transcript line (inclusive). */
  end: number
}

/**
 * Where each collapsible reasoning block lands in the flattened transcript, so
 * a click on its line can be mapped back to the block. Ranges are computed
 * with the same renderer that draws the transcript, so they stay exact even
 * while a block is growing or another collapses.
 */
export function reasoningRanges(blocks: Block[], width: number, options: RenderOptions = {}): BlockRange[] {
  const ranges: BlockRange[] = []
  let line = 0
  for (const [index, block] of blocks.entries()) {
    const rendered = renderBlock(block, width, options)
    if (rendered.length === 0) continue
    if (block.kind === "reasoning") ranges.push({ index, start: line, end: line + rendered.length - 1 })
    line += rendered.length
  }
  return ranges
}

/** Takes the last `count` lines (the visible window of a scrollback view). */
export function tail(lines: string[], count: number): string[] {
  if (count <= 0) return []
  if (lines.length <= count) return lines
  return lines.slice(lines.length - count)
}

/**
 * The transcript line a screen row maps to, for click hit-testing.
 *
 * `viewportTop` is the transcript line drawn at `viewportTopRow` (the 1-based
 * screen row where the transcript starts — slack pushes it down). A row above
 * the viewport is outside it and returns undefined, so a click can never
 * toggle a block it did not land on.
 */
export function transcriptLineAt(viewportTop: number, viewportTopRow: number, y: number): number | undefined {
  if (y < viewportTopRow) return undefined
  return viewportTop + (y - viewportTopRow)
}

/**
 * The visible window of the transcript. `scroll` counts lines up from the
 * bottom, so 0 is "live" (newest at the bottom) and 20 shows the region that
 * ended twenty lines ago.
 */
export function windowSlice(lines: string[], count: number, scroll = 0): string[] {
  if (count <= 0) return []
  const maxScroll = Math.max(lines.length - count, 0)
  const offset = Math.min(Math.max(scroll, 0), maxScroll)
  const end = lines.length - offset
  const start = Math.max(end - count, 0)
  return lines.slice(start, end)
}

export interface SidebarPanel {
  /** Lines, already fitted to `width`. */
  lines: string[]
  /** Panel content width; a one-column divider is added by the frame. */
  width: number
}

export interface FrameOptions {
  width: number
  height: number
  /** Top bar (one line normally). */
  header: string[]
  /** Session panel, drawn on the right of the transcript/overlay. */
  sidebar?: SidebarPanel
  /** Full transcript; the newest lines are shown. */
  transcript: string[]
  /** Palette / picker box, drawn just above the prompt box. */
  overlay: string[]
  /** The prompt box, borders included, each line exactly `width` wide. */
  prompt: string[]
  /** 1-based caret inside `prompt`. */
  promptCursor: { row: number; col: number }
  /** Status/hint line, below the prompt box. */
  status: string
  /** Lines scrolled up from the bottom of the transcript (0 = live). */
  scroll?: number
  /**
   * Absolute transcript line that should sit at the top of the viewport.
   * Takes precedence over `scroll`. Scrolling is driven by this index rather
   * than by a distance from the bottom, so output arriving while the user is
   * reading something earlier does not push it off the screen.
   */
  viewTop?: number
}

export interface Frame {
  /** Exactly `height` lines. */
  lines: string[]
  /** 1-based caret position, always inside the frame. */
  cursor: { row: number; col: number }
  /** 1-based row where the prompt box starts (for mouse hit-testing). */
  promptTop: number
  /** Transcript line index drawn on the first body row. */
  viewTop: number
  /** How many transcript lines are visible at once. */
  viewportRows: number
  /**
   * 1-based screen row where the first transcript line is drawn. Slack (a
   * transcript shorter than the viewport) pushes it down from the first body
   * row — click hit-testing needs the real row, not the body's first row.
   */
  viewportTopRow: number
}

/**
 * Lays out one full-screen frame:
 *
 *   ┌ header ────────────────────────────────────────────────┐
 *   │ transcript (+ overlay at the bottom)          │ panel   │
 *   ├─────────────────────────────────────────────────────────┤
 *   │ prompt box                                              │
 *   │ status                                                  │
 *   └─────────────────────────────────────────────────────────┘
 *
 * The prompt box and status line are never dropped — on a very short terminal
 * the header and the sidebar shrink first, then the transcript, so the user can
 * always see and edit what they are typing.
 */
export function composeFrame(options: FrameOptions): Frame {
  const width = Math.max(options.width, 1)
  const height = Math.max(options.height, 1)

  let header = [...options.header]
  let overlay = [...options.overlay]

  let prompt = options.prompt.length > 0 ? [...options.prompt] : [""]
  // The prompt can never be taller than the frame minus the status line.
  if (prompt.length > height - 1) prompt = prompt.slice(-(Math.max(height - 1, 1)))

  // Drop the overlay first, then the header, so the prompt always survives.
  while (header.length + prompt.length + 1 > height && overlay.length > 0) overlay.pop()
  while (header.length + prompt.length + 1 > height && header.length > 0) header.pop()

  const bodyRows = Math.max(height - header.length - prompt.length - 1, 0)
  if (overlay.length > bodyRows) overlay = overlay.slice(0, bodyRows)

  const sidebarWidth = options.sidebar && options.sidebar.width > 0 ? options.sidebar.width : 0
  const divider = sidebarWidth > 0 ? 1 : 0
  const contentWidth = Math.max(width - sidebarWidth - divider, 1)

  const viewportRows = Math.max(bodyRows - overlay.length, 0)
  const maxTop = Math.max(options.transcript.length - viewportRows, 0)
  let viewTop: number
  let viewport: string[]
  if (options.viewTop !== undefined && viewportRows > 0) {
    viewTop = Math.min(Math.max(Math.trunc(options.viewTop), 0), maxTop)
    viewport = options.transcript.slice(viewTop, viewTop + viewportRows)
  } else {
    const offset = Math.min(Math.max(options.scroll ?? 0, 0), maxTop)
    viewTop = maxTop - offset
    viewport = windowSlice(options.transcript, viewportRows, options.scroll ?? 0)
  }

  // Slack goes above the transcript so the newest line sits just above the
  // prompt box, the way a chat log reads.
  const body: string[] = []
  for (let i = 0; i < viewportRows - viewport.length; i++) body.push("")
  body.push(...viewport)
  body.push(...overlay)

  const lines: string[] = [...header].map((line) => padVisible(clampVisible(line, width), width))
  for (let i = 0; i < bodyRows; i++) {
    const content = padVisible(clampVisible(body[i] ?? "", contentWidth), contentWidth)
    if (sidebarWidth > 0) {
      // The panel sits on the right: the chat keeps the left edge, where the
      // eye starts, and the vital signs stay out of the way of the prose.
      const panel = padVisible(clampVisible(options.sidebar!.lines[i] ?? "", sidebarWidth), sidebarWidth)
      lines.push(padVisible(`${content}${theme.dim("\u2502")}${panel}`, width))
    } else {
      lines.push(padVisible(content, width))
    }
  }

  for (const line of prompt) lines.push(padVisible(clampVisible(line, width), width))
  lines.push(padVisible(clampVisible(options.status, width), width))
  while (lines.length < height) lines.push(" ".repeat(width))

  const cursorRow = header.length + bodyRows + options.promptCursor.row
  const promptTop = header.length + bodyRows + 1
  // Slack above the transcript pushes the first transcript line down the body
  // area; that exact row is what click hit-testing maps from.
  const slack = Math.max(viewportRows - viewport.length, 0)
  const viewportTopRow = header.length + 1 + slack
  return {
    lines: lines.slice(0, height),
    cursor: {
      row: Math.min(Math.max(cursorRow, 1), height),
      col: Math.min(Math.max(options.promptCursor.col, 1), width),
    },
    promptTop,
    viewTop,
    viewportRows,
    viewportTopRow,
  }
}
