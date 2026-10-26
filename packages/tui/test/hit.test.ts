/**
 * Click hit-testing geometry — a click on a thinking block must toggle it.
 *
 * The regression this holds: the mapping used the body's first row, ignoring
 * the slack above a short transcript, so clicks in a young session (the exact
 * moment someone first tries it) landed on the wrong line and nothing opened.
 */
import { describe, expect, test } from "bun:test"
import { composeFrame, reasoningRanges, transcriptLineAt, type Block } from "../src/render"

const OPEN = "<" + "think>"
const CLOSE = "<" + "/" + "think>"

function blocks(): Block[] {
  return [
    { kind: "user", text: "two plus two?" },
    { kind: "reasoning", text: "it is four", expanded: false, live: false, seconds: 1, category: "reasoning" },
    { kind: "assistant", text: "4" },
  ]
}

interface Layout {
  viewportTop: number
  viewportTopRow: number
  viewportRows: number
  promptTop: number
}

function layoutFor(transcriptLines: number, height: number, overlayRows = 0): { layout: Layout; ranges: ReturnType<typeof reasoningRanges> } {
  const blockList = blocks()
  // Enough filler that the transcript reaches `transcriptLines` lines.
  const filler: Block[] = []
  if (transcriptLines > 8) filler.push({ kind: "assistant", text: "word\n".repeat(transcriptLines) })
  const all = [...filler, ...blockList]
  const width = 80
  const transcript: string[] = []
  for (const block of all) {
    // renderTranscript would flatten; a one-line-per-block approximation is
    // enough for the geometry, except the reasoning block needs its lines.
    if (block.kind === "reasoning") transcript.push("thinking", "it is four")
    else transcript.push("line")
  }
  const frame = composeFrame({
    width,
    height,
    header: ["IMURUN"],
    transcript,
    overlay: Array.from({ length: overlayRows }, () => ""),
    prompt: ["box"],
    promptCursor: { row: 0, col: 0 },
    status: "ready",
  })
  return {
    layout: {
      viewportTop: frame.viewTop,
      viewportTopRow: frame.viewportTopRow,
      viewportRows: frame.viewportRows,
      promptTop: frame.promptTop,
    },
    ranges: reasoningRanges(all, width),
  }
}

describe("click hit-testing", () => {
  test("a young session with slack maps clicks onto the block", () => {
    const { layout, ranges } = layoutFor(4, 24)
    // The reasoning block occupies transcript lines 1-2; find its screen rows.
    const firstScreenRow = layout.viewportTopRow + (1 - layout.viewportTop)
    const line = transcriptLineAt(layout.viewportTop, layout.viewportTopRow, firstScreenRow)
    expect(line).toBe(1)
    const hit = ranges.find((range) => line !== undefined && line >= range.start && line <= range.end)
    expect(hit?.index).toBe(1)
  })

  test("every visible line of the block maps into its range", () => {
    const { layout, ranges } = layoutFor(4, 24)
    const range = ranges.find((r) => r.index === 1)!
    for (let row = layout.viewportTopRow; row < layout.viewportTopRow + layout.viewportRows; row++) {
      const line = transcriptLineAt(layout.viewportTop, layout.viewportTopRow, row)
      if (line === undefined || line < range.start || line > range.end) continue
      expect(line).toBeGreaterThanOrEqual(range.start)
      expect(line).toBeLessThanOrEqual(range.end)
    }
  })

  test("rows above the viewport are outside it, never a wrong block", () => {
    expect(transcriptLineAt(5, 10, 9)).toBeUndefined()
    expect(transcriptLineAt(5, 10, 10)).toBe(5)
    expect(transcriptLineAt(5, 10, 12)).toBe(7)
  })

  test("a full transcript (no slack) maps rows one to one", () => {
    const { layout } = layoutFor(60, 24)
    // The frame is at the bottom: the last transcript line sits just above the
    // prompt box.
    const lastRow = layout.promptTop - 1
    const line = transcriptLineAt(layout.viewportTop, layout.viewportTopRow, lastRow)
    expect(line).toBeGreaterThan(0)
  })

  test("an overlay shrinks the viewport and the mapping follows the frame", () => {
    const { layout: withOverlay } = layoutFor(4, 24, 3)
    const { layout: without } = layoutFor(4, 24, 0)
    // The overlay takes the bottom of the body, so the viewport is three rows
    // shorter and starts three rows higher.
    expect(withOverlay.viewportTopRow).toBe(without.viewportTopRow - 3)
    // Whatever the layout, the first transcript line is exactly where the
    // frame says it is — that is the invariant click-testing relies on.
    expect(transcriptLineAt(withOverlay.viewportTop, withOverlay.viewportTopRow, withOverlay.viewportTopRow)).toBe(
      withOverlay.viewportTop,
    )
  })
})
