# Research engine

`services/research/` is how IMURUN looks things up on the web: **no API key
required**, the **model decides** whether to search and writes the queries,
and what reaches the model is **cleaned, ranked passages with numbered
sources**, not raw search snippets. It backs the model's `web_search` tool
and the pre-fetch, and cleans pages for `web_fetch`.

```
user message (+ last 4 history messages)
  │
  ├─ pre-filter: greeting / thanks / code paste / long paste ─► no search, no generation
  │
  ├─ planner.py: ONE short routing generation with the same local model (≤ 60 tokens)
  │     {"web": true, "fresh": "live", "q": ["prime minister of India"], "ent": ["India"], "why": "..."}
  │     unusable output / no model ─► heuristic fallback (tools/policy.py regex: is_realtime + web intents)
  │     cached per (message, history) hash: intents() and observe_full() plan once
  │
  ├─ sources/ (parallel, per query; search phase ≤ half the deadline, 6 s per source)
  │     keyed (only if configured) · searxng_public · wikipedia · news_rss · direct · browser (opt-in)
  │
  ├─ extract.py (parallel fetch of the best results: one per domain, 4 pages, 8 s, 2 MB, HTML only)
  │
  ├─ rank.py: dedupe → chunk → BM25 + entities + trust + freshness → select under a token budget
  │
  └─ Context block + sources[] + research.* events
```

Code map:

| File | What |
|---|---|
| `planner.py` | `prefilter`, `parse_plan` (forgiving JSON via `tools/calls.py::loads_forgiving`), `heuristic_plan`, `generate_capped`, `Planner` (cache, in-flight de-duplication) |
| `sources/keyed.py` | Brave / Serper / Tavily / your own SearXNG — used first when a key/URL is configured, never required |
| `sources/searxng_public.py` | 12 public SearXNG instances, rotation, 10-minute failure memory, 2 in parallel |
| `sources/wikipedia.py` | direct title lookup + full-text search, REST summaries, plain-text body of the best article |
| `sources/rss.py` | Google News RSS search + BBC / NPR / Guardian / Al Jazeera feeds (filtered), `xml.etree` |
| `sources/direct.py` | URLs/domains named in the conversation, PyPI / npm / GitHub-releases JSON, a small map of official pages |
| `sources/browser_search.py` | client side of the embedded-browser helper protocol (`BROWSER-SEARCH.md`) |
| `extract.py` | readability-style main-content extraction and cleaning |
| `rank.py` | canonical URLs, shingles, BM25, trust tiers, freshness, selection, Context formatting |
| `engine.py` | `ResearchEngine.run(plan)` (time-boxed, never raises), `fetch_page`, `research_for_prompt(messages, model)` |
| `tool.py` | `ResearchToolMixin`; the concrete tool is `tools/builtin/web_search.py::WebSearchTool` (= `services.research.ResearchTool`) |
| `net.py` | the one HTTP client (httpx): honest User-Agent, size/time caps, public addresses only |

## Why each source

Measured from the development machine on 2026-11-01 with the engine's honest
User-Agent:

| Source | Why | Status here |
|---|---|---|
| **Wikipedia API** | Who holds an office, what a thing is, dates and figures — the summary of "Prime Minister of India" names the incumbent. Clean text, no fetch. Wikimedia requires an identifying User-Agent (a generic one gets 403). | works |
| **Google News RSS search** | The only keyless, query-driven *news* feed: headline, publisher, date. Item links are JavaScript redirects, so the headline + publisher + date is the passage; the chip shows the publisher's domain. | works |
| **Public news feeds** (BBC, NPR, Guardian, Al Jazeera) | Fresh articles that *can* be fetched and read; only items naming the question's entities are kept. Reuters and AP no longer publish public RSS, so they are not listed. | works |
| **Direct** | When the question names the place that holds the answer (python.org, a PyPI/npm package, a GitHub repo, India's PM site) a search engine is a detour. Registry APIs return exact versions and dates. | works; some official sites refuse non-browser clients (pmindia.gov.in → 403) |
| **Public SearXNG** | Real web results (meta-search) with a JSON API. Instances come and go, rate-limit and put bot challenges in front, hence rotation + failure memory. | **0 of 40 probed instances served JSON** (429 or an Anubis challenge page) — best effort |
| **Embedded browser** | A real browser session the user owns; see `BROWSER-SEARCH.md`. Off by default. | not available until `window.py --search-helper` exists |
| **Keyed** | Reliable general web search when the user adds a key (Brave has a free tier). | optional |

Deliberately **not** used: Google web search HTML (blocked and against its
terms); DuckDuckGo's HTML endpoint over plain HTTP (it answers this client
with a bot-check page; the engine does not disguise itself as a browser to
get past that); Bing's `format=rss` output (its terms limit it to personal RSS
readers); Mojeek (captcha). A refusal is always reported as a refusal
(`blocked`), never retried with a disguise, and results are never invented.

## Cleaning rules (`extract.py`)

1. Parse with `html.parser` into a small tree, applying the browser's implied
   end tags (`<p>`, `<li>`, cells, rows). Collect metadata while parsing.
2. Drop: `script style noscript template svg math iframe canvas form button
   input select textarea nav aside menu dialog object embed video audio picture
   map head label option`; `header`/`footer` outside `article`/`main`;
   `hidden`, `aria-hidden="true"`, `display:none`; ARIA roles navigation,
   banner, contentinfo, complementary, search, dialog, alert, menu(bar),
   toolbar; any element whose class/id says cookie / consent / banner / share /
   social / subscribe / newsletter / promo / ad(s) / sponsor / related /
   recommended / sidebar / breadcrumbs / masthead / nav(bar) / menu / popup /
   modal / signup / login / paywall / outbrain / taboola / footer / header /
   toolbar / widget / pagination … **unless** it also says article / body /
   content / entry / main / post / text / story / answer / question / message /
   thread / prose ….
3. Score paragraph-like blocks (`p pre td blockquote dd li`, leaf `div`s):
   `1 + commas + min(len/100, 3)` for blocks ≥ 25 characters; the parent gets
   the full score, the grandparent half; containers get a tag bonus (div +5,
   article/main +10, headings −5 …) and ±25 for class/id; the result is
   multiplied by `1 − link density`. The best container is widened to any
   ancestor holding ≥ 1/0.7× its good prose with link density < 0.33 (a forum
   thread is several posts).
4. Render: `#` headings, paragraphs, `-` / `1.` lists (nested indented),
   Markdown tables for 2+ column tables (else cell text), `>` quotes, fenced
   `pre`, `**term**` for `dt`.
5. Clean each line: entities decoded, NBSP / zero-width characters removed,
   whitespace collapsed; drop lines that are only symbols, lines made only of
   UI words ("Reply Quote Share", "Facebook Twitter Email"), short lines
   matching boilerplate ("we use cookies", "subscribe", "share on", "all
   rights reserved", "advertisement", "read more", "related articles", "skip to
   content", "enable JavaScript", "privacy policy" …) and exact repeats.
6. If the chosen container yields < 200 characters, the whole cleaned body is
   used instead when it is longer.

Metadata: title (`og:title` → `twitter:title` → `<title>`), published date
(`article:published_time` and 14 other meta names, `itemprop=datePublished`,
JSON-LD `datePublished`, first `<time datetime>`, then modified dates) as ISO
8601, language (`<html lang>`, else script detection + stop-word counts for
en/es/fr/de/pt/it/nl), description, site name, canonical URL, `fetched_at`
(UTC).

## Ranking (`rank.py`)

- **Dedupe hits** by canonical URL: https, no `www.`/`m.`/`amp.`, no
  fragment, no `utm_*`/`fbclid`/`gclid`/… parameters, no `/amp` or
  `index.html`, no trailing slash. **Dedupe pages** whose first 3000
  characters share ≥ 80 % of their 5-word shingles (syndicated copies).
- **Chunk** pages into ~700-character passages on paragraph boundaries, each
  carrying its nearest heading; long paragraphs split on sentences.
- **Score** each passage. Live questions (`fresh: live`):
  `0.45 BM25 + 0.10 entity coverage + 0.15 trust + 0.20 freshness + 0.05
  current-year mention + 0.10 present-tense cue` ("current", "incumbent",
  "serving since", "as of 2026" …). Otherwise:
  `0.55 BM25 + 0.15 entities + 0.15 trust + 0.10 freshness + 0.05 year`.
  BM25 is the best of the planned queries, normalised. Snippet-only passages
  ×0.8; a passage that matches no query term ×0.3. "current", "latest",
  "now" are *not* stop words.
- **Trust tiers**: 1.0 official/reference (`.gov`, `.gov.xx`, `.edu`, `.int`,
  Wikipedia, Britannica, Reuters, AP, BBC, python.org, MDN, GitHub, PyPI …);
  0.75 major newsrooms and Stack Overflow; 0.45 everything else; 0.15 social
  media and user-generated aggregators.
- **Freshness** (live): ≤ 2 days 1.0, ≤ 7 d 0.8, ≤ 30 d 0.6, ≤ 1 y 0.3, older
  0, unknown 0.3. Wikipedia passages carry no date (the API's timestamp is
  the last edit, not when the facts were true).
- **Select** under the token budget (`chars / 3.5`, as in
  `response/context.py`): first the best passage for each planned query, then
  the rest above `max(0.2, 0.45 × best)`; ≤ 2 passages per source; ≤ 3
  headline-only passages; passages sharing ≥ 60 % shingles dropped; each
  passage ≤ 900 characters.

The Context block:

```
Web research for: <queries> (searched 2026-11-01 16:23 UTC)
Sources:
[1] Prime Minister of India — en.wikipedia.org
[2] Prime Minister's Office (India) — en.wikipedia.org
Passages:
[1] ... Narendra Modi, who is the current prime minister of India, serving since 26 May 2014. ...
[2] ...
Cite the passages you use as [n]. For facts about the present use only these passages; if they do not answer the question, say so in one short sentence.
```

`data["sources"]` holds `{title, url, domain, provider, snippet}` per numbered
source, which `response/loop.py::collect_sources` turns into the source chips
and the `verified` badge. `data["results"]` keeps the old hit list for
`web_search.done`.

## Budgets

| What | Default | Variable |
|---|---|---|
| Whole research run | 12 s | `IMURUN_RESEARCH_DEADLINE` (2–60) |
| Search phase | half the deadline | — |
| One search source | 6 s | `IMURUN_RESEARCH_SEARCH_TIMEOUT` |
| One page | 8 s, 2 MB, HTML only | `IMURUN_RESEARCH_FETCH_TIMEOUT` |
| Pages fetched | 4 (one per domain) | `IMURUN_RESEARCH_PAGES` (0–10) |
| Context block | 900 tokens (≈ 3150 chars) | `IMURUN_RESEARCH_TOKENS` |
| Planner output | 60 tokens | `IMURUN_RESEARCH_PLANNER_TOKENS` |
| Planner wall clock | 8 s (stream path) | `IMURUN_RESEARCH_PLANNER_TIMEOUT` |
| Planner on/off | on | `IMURUN_RESEARCH_PLANNER=0` → heuristic only |
| Public SearXNG | on | `IMURUN_RESEARCH_SEARXNG_PUBLIC=0`; list: `IMURUN_SEARXNG_INSTANCES` |
| Browser helper | off | `IMURUN_BROWSER_SEARCH=1` |
| News locale | en-US | `IMURUN_RESEARCH_LOCALE` (e.g. `en-IN`) |

Measured live (2026-11-01, this machine, heuristic plan): "who is the current
prime minister of India" → 3.0–3.2 s end to end, Wikipedia + news RSS +
direct answered, SearXNG refused, the top passage names the incumbent.

The planner's cost: with a model client that accepts `max_tokens` it is one
≤ 60-token generation; otherwise it streams and stops when the JSON object
closes (closing the stream cancels the engine, `fish_native._stream`). It
does **not** share the main prompt's prefix, so the engine's prefix cache is
re-filled for the answer; on CPU with a small model expect a few hundred ms to
~2 s extra per non-trivial message. The pre-filter removes the cost for
greetings, thanks and code pastes.

## Privacy: what leaves the machine

- **To the search sources**: the planned queries (written by the model from
  your message and the last few turns), and nothing else — no cookies, no
  account, no message history. Wikipedia, Google News, public SearXNG
  instances (volunteer-run; each sees the queries sent to it), the RSS feed
  hosts (they see a feed download, not your query), PyPI / npm / GitHub for
  package names, and keyed providers when you configured one.
- **To result pages**: a normal GET of up to 4 pages per research run, with
  the IMURUN User-Agent. The engine never sends forms, never runs page
  scripts, refuses `localhost`, private, loopback and link-local literal
  addresses, and caps every download.
- **Locally**: plans are cached in memory for 5 minutes (64 entries), feeds
  for 5 minutes, SearXNG failures for 10 minutes. Nothing is written to
  disk. `research.*` events go only to the local UI; `research.plan` events
  carry the queries, not your message.

## Terms

Feeds (Google News, publishers) are offered for personal, non-commercial
reading; IMURUN fetches them on the user's own machine, for that user. A
hosted or commercial deployment should use a keyed provider instead. The
browser helper is off by default for the same reason (search engines'
terms restrict automated use of their result pages); see `BROWSER-SEARCH.md`.

## Integration

- `services/tools/builtin/web_search.py::WebSearchTool` is the research tool
  (`name="web_search"`; `query` required, optional `queries` up to 3). Keyed
  providers are used first when configured.
- `services/tools/builtin/web_fetch.py::WebFetchTool` fetches through
  `engine.fetch_page` and cleans with `extract.py` (main content, published
  date, language). It now refuses local/private addresses.
- `services/tools/runner.py::ToolRunner`: `intents(prompt, history=, model=,
  publish=, turn_id=)` adds `web_search` when the plan says `needs_web`;
  weather / time / location keep their regex intents. `observe_full(...)`
  runs the plan through `WebSearchTool.run_plan`. Without a model argument
  the runner uses `planner_model` (if the server set it), else the heuristic.
- Events: `research.plan`, `research.search` (per source × query: `ok`,
  `results`, `error`, `blocked`, `elapsed`), `research.fetch` (`url`,
  `status`, `ok`, `chars`, `error`, `lang`, `published`), `research.done`
  (`sources`, `passages`, `hits`, `errors`, `offline`, `blocked`, `elapsed`).
  A model-issued `web_search` also emits `web_search.start` / `web_search.done`;
  for the pre-fetch the loop emits those itself. All carry `turn_id`.
- The loop hook that makes the pre-fetch model-planned is in `REQUESTS.md`.

## Tests

`tests/test_research_planner.py`, `test_research_extract.py` (fixtures in
`tests/fixtures/research/`), `test_research_rank.py`,
`test_research_sources.py` (fake HTTP layer; a fake helper process speaks the
browser protocol over real pipes), `test_research_pipeline.py` (whole
pipeline with the network faked; deadline; offline / blocked / empty). The
live smoke test is skipped by default:

```
$env:IMURUN_NETWORK_TESTS="1"; python -m pytest -m network -s tests/test_research_pipeline.py
```
