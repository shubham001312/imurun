/**
 * The reasoning engine — thinking stays out of the answer, live.
 *
 * Every case here is a real shape seen from small local models: split tags,
 * unclosed blocks, tags inside code fences, uppercase tags, several blocks in
 * one answer. The tags are built from pieces so this file never contains a
 * literal one — the same mistake the tests exist to catch.
 */
import { describe, expect, test } from "bun:test"
import { ReasoningSplitter, stripThinking } from "../src/agent/reasoning"

const OPEN = "<" + "think>"
const CLOSE = "<" + "/" + "think>"
const FENCE = "```"

/** Feeds a whole string at once and returns the split result. */
function once(text: string): { text: string; reasoning: string } {
  const splitter = new ReasoningSplitter()
  const first = splitter.push(text)
  const tail = splitter.flush()
  return {
    text: [first.text, tail.text].filter(Boolean).join(""),
    reasoning: [first.reasoning, tail.reasoning].filter(Boolean).join(""),
  }
}

/** Feeds a string in `size`-character chunks, as a stream would. */
function chunked(text: string, size: number): { text: string; reasoning: string } {
  const splitter = new ReasoningSplitter()
  let textOut = ""
  let thinkOut = ""
  for (let at = 0; at < text.length; at += size) {
    const split = splitter.push(text.slice(at, at + size))
    textOut += split.text ?? ""
    thinkOut += split.reasoning ?? ""
  }
  const tail = splitter.flush()
  return { text: textOut + (tail.text ?? ""), reasoning: thinkOut + (tail.reasoning ?? "") }
}

describe("the common pair", () => {
  test("reasoning before the answer is lifted out", () => {
    const { text, reasoning } = once(`${OPEN}the user wants X${CLOSE}The answer is X.`)
    expect(reasoning).toBe("the user wants X")
    expect(text).toBe("The answer is X.")
  })

  test("a tag split across chunks is still matched", () => {
    // "<thi" then "nk>": the tag itself arrives in two pieces.
    const { text, reasoning } = chunked(`${OPEN.slice(0, 4)}${OPEN.slice(4)}plan${CLOSE}done`, 2)
    expect(reasoning).toBe("plan")
    expect(text).toBe("done")
  })

  test("an unclosed block is reasoning, never a half-answer", () => {
    const { text, reasoning } = once(`Let me see.${OPEN}step one, step two`)
    expect(text).toBe("Let me see.")
    expect(reasoning).toBe("step one, step two")
  })

  test("text before the block is kept", () => {
    const { text, reasoning } = chunked(`Sure!\n\n${OPEN}\nplans\n${CLOSE}\n\nAnswer.`, 4)
    expect(text).toBe("Sure!\n\n\n\nAnswer.")
    expect(reasoning).toContain("plans")
  })

  test("several blocks in one answer all separate", () => {
    const { text, reasoning } = once(
      `${OPEN}one${CLOSE}First part. ${OPEN}two${CLOSE}Second part.`,
    )
    expect(text).toBe("First part. Second part.")
    expect(reasoning).toBe("onetwo")
  })

  test("case is ignored, and space before the bracket tolerated", () => {
    const { text, reasoning } = once(`<THINK >hmm</THINK >Fine.`)
    expect(reasoning).toBe("hmm")
    expect(text).toBe("Fine.")
  })

  test("the other names (thinking, reasoning, thought) work too", () => {
    for (const name of ["thinking", "reasoning", "thought"]) {
      const { text, reasoning } = once(`<${name}>why</${name}>Answer.`)
      expect(reasoning).toBe("why")
      expect(text).toBe("Answer.")
    }
  })
})

describe("code fences", () => {
  test("a tag inside a fence is content, not a block", () => {
    const source = `Use this:\n${FENCE}txt\n${OPEN}example${CLOSE}\n${FENCE}\nDone.`
    const { text, reasoning } = once(source)
    expect(reasoning).toBe("")
    expect(text).toBe(source)
  })

  test("the same, arriving in awkward chunk sizes", () => {
    const source = `${FENCE}js\n// ${OPEN}\nconst x = 1;\n${FENCE}\nResult.`
    for (const size of [1, 2, 3, 5, 8, 64]) {
      const { text, reasoning } = chunked(source, size)
      expect(reasoning).toBe("")
      expect(text).toBe(source)
    }
  })

  test("a fence inside a think block is reasoning", () => {
    const { text, reasoning } = once(`${OPEN}${FENCE}js\nx()${FENCE}${CLOSE}Answer.`)
    expect(text).toBe("Answer.")
    expect(reasoning).toContain("x()")
  })
})

describe("ordinary text is never delayed or mangled", () => {
  test("angle brackets that are not tags pass through", () => {
    const source = "if (a < b && c > d) return 1;"
    const { text, reasoning } = once(source)
    expect(reasoning).toBe("")
    expect(text).toBe(source)
  })

  test("a lone `<` at a chunk boundary still arrives", () => {
    const { text } = chunked("compare a < b carefully", 5)
    expect(text).toBe("compare a < b carefully")
  })

  test("plain answers stream through untouched", () => {
    const source = "Just the answer, no thinking."
    const { text, reasoning } = once(source)
    expect(text).toBe(source)
    expect(reasoning).toBe("")
  })
})

describe("stripThinking — the finalize pass", () => {
  test("removes complete and unclosed blocks", () => {
    expect(stripThinking(`${OPEN}a${CLOSE}Answer`)).toBe("Answer")
    expect(stripThinking(`Answer with ${OPEN}x more`)).toBe("Answer with ")
  })

  test("leaves clean text and code alone", () => {
    const source = `Plain.\n${FENCE}xml\n${OPEN}example${CLOSE}\n${FENCE}\nEnd.`
    expect(stripThinking(source)).toBe(source)
  })
})
