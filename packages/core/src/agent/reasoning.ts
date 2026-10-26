/**
 * The reasoning engine — separates a model's thinking from its answer, live.
 *
 * Local models do not all have a separate reasoning channel. The common ones
 * (Qwen, DeepSeek and their distills) open a ` think` block in the ordinary
 * text stream and close it with ` inline`. That reasoning is worth showing —
 * collapsed, and never as the answer — so it is pulled out as the deltas
 * arrive, including the awkward case where the tag itself is split across two
 * deltas (`<thi` then `nk>`).
 *
 * Three rules beyond the tag match, each one a real failure seen from small
 * models:
 *
 *   1. **Tags inside a code fence are content.** An answer that documents the
 *      format must not swallow the rest of the reply as "thinking". A ``` fence
 *      outside a block toggles a code state, and tags inside are passed through.
 *   2. **Tags are matched loosely in shape, strictly in meaning.** Case is
 *      ignored, and whitespace before the closing angle bracket is tolerated
 *      (`<Think >`, `</THINK>`), because small models emit both. An unknown
 *      `<something>` is ordinary text.
 *   3. **An unclosed block is still thinking.** A model that runs out of output
 *      mid-block was reasoning, not answering — `flush` files it as reasoning
 *      so the transcript never shows a half-thought as the answer.
 */

const OPEN_NAMES = ["think", "thinking", "reasoning", "thought"] as const
const CLOSE_NAMES = ["think", "thinking", "reasoning", "thought"] as const

export interface SplitResult {
  /** Text for the user's transcript, or undefined when there is none. */
  text?: string
  /** Text for the collapsed thinking block, or undefined when there is none. */
  reasoning?: string
}

/** How much of a tag could still be arriving: the longest tag's tail length. */
const MAX_TAG_TAIL = 12 // "</reasoning" is the longest partial that matters

export class ReasoningSplitter {
  /** Undefined while outside a block; the name being closed while inside one. */
  private closing: (typeof CLOSE_NAMES)[number] | undefined
  /** Characters held back because they may be the start of a tag. */
  private held = ""
  /** True between a ``` fence and its closing fence (outside a block). */
  private inFence = false

  get insideThink(): boolean {
    return this.closing !== undefined
  }

  /**
   * Feeds a delta and returns what can be shown. Call `flush` when the stream
   * ends so nothing held back is lost.
   */
  push(delta: string): SplitResult {
    if (!delta) return {}
    let buffer = this.held + delta
    this.held = ""

    let text = ""
    let reasoning = ""

    while (buffer.length > 0) {
      if (this.closing) {
        const close = findTag(buffer, "close", this.closing)
        if (close) {
          reasoning += buffer.slice(0, close.start)
          buffer = buffer.slice(close.end)
          this.closing = undefined
          continue
        }
        // Hold back only a possible partial closing tag; the rest is reasoning.
        const keep = partialTail(buffer, "close")
        reasoning += buffer.slice(0, buffer.length - keep)
        this.held = buffer.slice(buffer.length - keep)
        return finish(text, reasoning)
      }

      // Inside a code fence, everything — think tags included — is content
      // until the fence closes. An answer that documents the format must not
      // swallow the rest of the reply as "thinking".
      if (this.inFence) {
        const close = buffer.indexOf("```")
        if (close >= 0) {
          text += buffer.slice(0, close + 3)
          this.inFence = false
          buffer = buffer.slice(close + 3)
          continue
        }
        const keep = partialTail(buffer, "fence")
        text += buffer.slice(0, buffer.length - keep)
        this.held = buffer.slice(buffer.length - keep)
        return finish(text, reasoning)
      }

      // Outside a block: whichever comes first wins.
      const fence = buffer.indexOf("```")
      const open = findTag(buffer, "open")
      if (open && (fence === -1 || open.start < fence)) {
        text += buffer.slice(0, open.start)
        buffer = buffer.slice(open.end)
        this.closing = open.name
        continue
      }
      if (fence >= 0) {
        text += buffer.slice(0, fence + 3)
        this.inFence = true
        buffer = buffer.slice(fence + 3)
        continue
      }

      // No opening tag and no fence in sight: emit up to the point a partial
      // tag or fence could start.
      const keep = Math.max(partialTail(buffer, "open"), partialTail(buffer, "fence"))
      text += buffer.slice(0, buffer.length - keep)
      this.held = buffer.slice(buffer.length - keep)
      return finish(text, reasoning)
    }
    return finish(text, reasoning)
  }

  /**
   * Ends the stream: anything still held becomes visible text outside a block,
   * or reasoning inside one — an unclosed ` think` is still reasoning.
   */
  flush(): SplitResult {
    if (this.held) {
      const pending = this.held
      this.held = ""
      return this.insideThink ? { reasoning: pending } : { text: pending }
    }
    return {}
  }
}

export interface TagHit {
  name: (typeof OPEN_NAMES)[number]
  start: number
  end: number
}

/**
 * Finds the first open or close tag in `buffer`.
 *
 * The match is case-insensitive and tolerates spaces before `>`, but the name
 * has to be exactly one of the known ones — an unknown `<something>` is text,
 * and so is every `<` that is not followed by a known name.
 */
function findTag(
  buffer: string,
  kind: "open" | "close",
  only?: (typeof CLOSE_NAMES)[number],
): TagHit | undefined {
  const lower = buffer.toLowerCase()
  const names = kind === "open" ? OPEN_NAMES : CLOSE_NAMES
  let best: TagHit | undefined
  for (const name of names) {
    if (only && name !== only) continue
    const needle = kind === "open" ? `<${name}` : `</${name}`
    let at = lower.indexOf(needle)
    while (at >= 0) {
      // Skip past the name, then any spaces, then require the closing bracket.
      let cursor = at + needle.length
      while (cursor < buffer.length && (buffer[cursor] === " " || buffer[cursor] === "\t")) cursor++
      if (buffer[cursor] === ">") {
        const hit: TagHit = { name, start: at, end: cursor + 1 }
        if (!best || hit.start < best.start) best = hit
        break
      }
      at = lower.indexOf(needle, at + 1)
    }
  }
  return best
}

function finish(text: string, reasoning: string): SplitResult {
  return {
    ...(text ? { text } : {}),
    ...(reasoning ? { reasoning } : {}),
  }
}

/**
 * How many characters at the end of `buffer` could be the start of one of the
 * shapes we look for? At most the longest prefix — and a tail is only held
 * back when it genuinely matches one, so ordinary text is never delayed.
 */
function partialTail(buffer: string, kind: "open" | "close" | "fence"): number {
  const start = Math.max(buffer.length - MAX_TAG_TAIL, 0)
  const lower = buffer.toLowerCase()
  for (let at = start; at < buffer.length; at++) {
    const tail = lower.slice(at)
    if (kind === "fence") {
      if ("```".startsWith(tail) || tail.startsWith("```")) return buffer.length - at
      continue
    }
    const names = kind === "open" ? OPEN_NAMES : CLOSE_NAMES
    const prefix = kind === "open" ? "<" : "</"
    if (prefix.startsWith(tail) || tail.startsWith(prefix)) return buffer.length - at
    if (names.some((name) => (prefix + name).startsWith(tail))) return buffer.length - at
  }
  return 0
}

/**
 * Removes complete think blocks from finished text.
 *
 * This is the finalize pass: whatever reaches the transcript, the session
 * store, a digest or `/copy` has already been through the splitter, but any
 * path that receives model text directly (a subagent report, a continuation)
 * gets the same guarantee by calling this. A block the model never closed is
 * dropped too — a half-thought is never an answer.
 */
export function stripThinking(text: string): string {
  if (!text) return text
  const splitter = new ReasoningSplitter()
  const split = splitter.push(text)
  const tail = splitter.flush()
  return [split.text, tail.text].filter((part): part is string => Boolean(part)).join("")
}
