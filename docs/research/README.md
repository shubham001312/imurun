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
  ├─ routing.py: topics of the plan (general · code · science · news · release), regex, instant
  │
  ├─ sources/ (parallel; search phase ≤ half the deadline, 6 s per source, less where capped)
  │     whole web:  browser (embedded browser: DuckDuckGo, Bing fallback) · mwmbl · searxng_public
  │     reference:  wikipedia · ddg_instant
  │     code:       stackexchange · github_search · hackernews
  │     science:    arxiv · openalex
  │     release:    endoflife
  │     news:       news_rss
  │     named:      direct (URLs, PyPI / npm / GitHub releases)
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
| `routing.py` | `topics(plan)`: which kinds of source a plan needs (`Source.topics` gates each source) |
| `sources/__init__.py` | `Source` (`per_plan`, `topics`, `timeout_cap`), `ApiSource` (JSON GET + failure memory), `Backoff`, `default_sources()` |
| `sources/browser_search.py` | the embedded-browser helper client (`BROWSER-SEARCH.md`): DuckDuckGo, Bing only when DuckDuckGo refuses / fails / finds nothing; Bing and DuckDuckGo redirect links unwrapped; per-engine refusal memory; idle shutdown |
| `sources/mwmbl.py` | Mwmbl, the open non-profit web index (`api.mwmbl.org/search`); results are pages to fetch |
| `sources/searxng_public.py` | public SearXNG instances, rotation, 10-minute failure memory, 2 in parallel, 4 s cap |
| `sources/wikipedia.py` | direct title lookup + full-text search, REST summaries, plain-text body of the best article |
| `sources/ddg_instant.py` | DuckDuckGo Instant Answer API: abstract + infobox for the plan's entities |
| `sources/stackexchange.py` | code: Stack Overflow / Ask Ubuntu / Super User — question + accepted (else top) answer as text |
| `sources/github_search.py` | code: GitHub issues for error messages, repositories when a project is asked for |
| `sources/hackernews.py` | code / tech: Hacker News stories (Algolia), the linked article is fetched |
| `sources/arxiv.py` | science: arXiv Atom API, title + authors + abstract |
| `sources/openalex.py` | science: OpenAlex works, abstract rebuilt from the inverted index, DOI link |
| `sources/endoflife.py` | release: endoflife.date cycles (release date, latest patch, support, EOL) |
| `sources/rss.py` | news: Google News RSS search + BBC / NPR / Guardian / Al Jazeera feeds (filtered), `xml.etree` |
| `sources/direct.py` | URLs/domains named in the conversation, PyPI / npm / GitHub-releases JSON, a small map of official pages |
| `sources/keyed.py` | developer-only environment-variable path (not in the UI or user docs) |
| `extract.py` | readability-style main-content extraction and cleaning |
| `rank.py` | canonical URLs, shingles, BM25, trust tiers, freshness, selection, Context formatting |
| `engine.py` | `ResearchEngine.run(plan)` (time-boxed, never raises), `fetch_page`, `research_for_prompt(messages, model)` |
| `tool.py` | `ResearchToolMixin`; the concrete tool is `tools/builtin/web_search.py::WebSearchTool` (= `services.research.ResearchTool`) |
| `net.py` | the one HTTP client (httpx): honest User-Agent, size/time caps, public addresses only |

## Why each source

Web search never needs an API key. Measured from the development machine
(Windows 11, home connection) on **2026-11-09** with the engine's honest
User-Agent; "time" is one request:

| Source | Route | Why | Status 2026-11-09 |
|---|---|---|---|
| **Embedded browser** (`browser`) | general | The whole web. The app's own WebView (hidden, private mode) loads a DuckDuckGo results page the way the user's browser would; Bing only when DuckDuckGo refuses, fails or finds nothing. See `BROWSER-SEARCH.md`. On by default. | **works**: helper ready in 1.3 s; DuckDuckGo 2.7 s cold / 1.2-1.7 s warm, Bing 0.8-1.1 s, 10 results each, no challenge |
| **Mwmbl** (`mwmbl`) | general | Open, non-profit, community-crawled web index with a JSON API (the one mwmbl.org and its extension use). Smaller and less fresh than a commercial index; its hits are pages to read. No published API terms found (repo AGPL-3.0); used gently: one request per query, failure memory. | works: 200 JSON, 0.4-1.5 s |
| **Public SearXNG** (`searxng_public`) | general | Meta-search JSON API on volunteer instances. | **1 of 77** searx.space instances served JSON (search.lumy.live); 51 answered 429, the rest a challenge page, HTML or 403 (2026-11-01: 0 of 40). Best effort, 4 s cap |
| **Wikipedia API** | general | Who holds an office, what a thing is, dates and figures. Clean text, no fetch. Wikimedia requires an identifying User-Agent. | works |
| **DuckDuckGo Instant Answer API** (`ddg_instant`) | general | DuckDuckGo's documented keyless JSON API (not its result pages): abstract + infobox for a *name* ("Jensen Huang" → "co-founder, president, and CEO of Nvidia"). Whole questions get an empty answer, so it is asked for the plan's entities. `t=imurun` identifies the app. (Its terms page could not be fetched today; attribution is kept: the chip links the abstract's source.) | works: 0.1-0.4 s |
| **Stack Exchange API** (`stackexchange`) | code | Question + accepted (else top-voted) answer as clean text; Ask Ubuntu / Super User when the question is about those. CC BY-SA, chip links the question. Keyless quota 300 requests/day/IP; `backoff` honoured. | works: 0.4-0.8 s |
| **GitHub search API** (`github_search`) | code | Issues for error messages; repositories only when a library/tool is asked for (repository search for "latest Python release" returns repos *named* like that). 10 searches/min unauthenticated; parked until the reset time on 403/429. | works: 0.9 s |
| **Hacker News (Algolia)** (`hackernews`) | code | Tech stories; the linked article is fetched. | works: 1.3 s |
| **arXiv API** (`arxiv`) | science | Preprints with abstracts. arXiv asks for ≤ 1 request / 3 s: once per plan. | works: 1.2 s |
| **OpenAlex** (`openalex`) | science | Every field's published works with abstracts (covers Crossref). Without a key one IP shares a small daily budget (the API says so). | works: 2.7 s |
| **endoflife.date** (`endoflife`) | release | Release cycles: release date, latest patch + date, support, EOL — "latest Python release" → 3.15, latest 3.15.2 (2026-10-25). | works: 0.5-2.6 s |
| **Google News RSS search** | news | Keyless, query-driven news: headline, publisher, date. Links are JavaScript redirects: headline + publisher + date is the passage. | works |
| **Public news feeds** (BBC, NPR, Guardian, Al Jazeera) | news | Fresh articles that can be fetched; only items naming the question's entities are kept. | works |
| **Direct** | named | URLs / domains named in the conversation, PyPI / npm / GitHub-releases JSON, a few official pages. | works; some official sites refuse non-browser clients (pmindia.gov.in → 403) |

Probed and **not** used (2026-11-09):

| Endpoint | Result | Why not |
|---|---|---|
| Marginalia Search API (`api.marginalia.nu/public/…`, `api2.marginalia-search.com`) | old path timed out (20 s); new API needs an `API-Key` header, and the shared `public` key answered **429 "QPM Limit Exceeded"** | unusable without a personal key |
| Reddit JSON (`reddit.com/search.json`) | **403** HTML | refuses non-browser clients |
| Semantic Scholar | **429** "apply for a key" | rate-limited without a key; OpenAlex covers the need |
| Wiby (`wiby.me/json`) | 200 JSON | "small web" index: hobby pages, rarely the answer |
| Wikidata `wbsearchentities` | 200 JSON | entity ids only; Wikipedia + DDG abstracts already carry the facts |
| Crossref | 200 JSON | no abstracts; OpenAlex includes Crossref's works |
| MDN search API | 200 JSON | works, not routed yet (MDN pages already rank tier 1 when the web search finds them) |
| Wikinews | 200 JSON | stale for current events |
| GDELT doc API | timed out | |
| Europe PMC | read timed out (10 s) | |
| Lobsters search | 500 | |

Deliberately **not** used: Google web search HTML over plain HTTP (blocked and
against its terms); DuckDuckGo's HTML endpoint *over plain HTTP* (it answers
this client with a bot-check page; the engine does not disguise its HTTP
client as a browser to get past that — the embedded browser is the user's
own browser engine, used the way the user would, see `BROWSER-SEARCH.md`);
Bing's `format=rss` output (its terms limit it to personal RSS readers);
Mojeek (captcha). A refusal is always reported as a refusal (`blocked`),
never retried with a disguise, and results are never invented.

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
  Wikipedia, Britannica, Reuters, AP, BBC, python.org, MDN, GitHub, PyPI,
  official language / tool docs: learn.microsoft.com, docs.rs, go.dev,
  kubernetes.io, postgresql.org …); 0.75 major newsrooms, Stack Overflow and
  the other Stack Exchange sites, arXiv, doi.org, OpenAlex, endoflife.date,
  cppreference; 0.45 everything else; 0.15 social media and user-generated
  aggregators. When choosing pages to fetch, a result's source also weighs
  (`SOURCE_WEIGHT`: browser results 0.9, SearXNG 0.8, Mwmbl 0.7, HN 0.6).
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
| One search source | 6 s; capped lower per source (`timeout_cap`: Mwmbl, Stack Exchange, GitHub, HN, endoflife 5 s; DDG Instant 4 s; public SearXNG 4 s) | `IMURUN_RESEARCH_SEARCH_TIMEOUT` |
| One page | 8 s, 2 MB, HTML only | `IMURUN_RESEARCH_FETCH_TIMEOUT` |
| Pages fetched | 4 (one per domain) | `IMURUN_RESEARCH_PAGES` (0–10) |
| Context block | 900 tokens (≈ 3150 chars) | `IMURUN_RESEARCH_TOKENS` |
| Planner output | 60 tokens | `IMURUN_RESEARCH_PLANNER_TOKENS` |
| Planner wall clock | 8 s (stream path) | `IMURUN_RESEARCH_PLANNER_TIMEOUT` |
| Planner on/off | on | `IMURUN_RESEARCH_PLANNER=0` → heuristic only |
| Public SearXNG | on | `IMURUN_RESEARCH_SEARXNG_PUBLIC=0`; list: `IMURUN_SEARXNG_INSTANCES` |
| Browser helper | **on** | `IMURUN_BROWSER_SEARCH=0` switches it off |
| Browser helper idle exit | 600 s | `IMURUN_BROWSER_SEARCH_IDLE` (30–86400) |
| Failure memory | refusal (403 / 429, quota, `backoff`): 10 min or the server's `Retry-After` / reset (≤ 1 h); 3 errors in a row: 2 min | — |
| News locale | en-US | `IMURUN_RESEARCH_LOCALE` (e.g. `en-IN`) |

Measured live, end to end (heuristic plan, this machine, 2026-11-09). Before =
the committed code of that morning (browser off by default), after = this
version with the browser helper on:

| Question | Before: sources that answered / top passages | After: sources that answered / top passages |
|---|---|---|
| latest Python release | Wikipedia, news RSS, direct; 2.7 s. Top: Wikipedia history ("Python 3.0 was released on 3 December 2008"), then an unrelated BBC crime story | + browser, Mwmbl, endoflife, Stack Exchange, HN, public SearXNG (lumy); 4.7 s. Top: **endoflife.date "python 3.15, released 2026-10-09, latest version 3.15.2 (2026-10-25)"**; python.org/downloads, docs.python.org/3/whatsnew/3.15, blog.python.org fetched |
| who is the CEO of Nvidia | Wikipedia only; 3.7 s; one passage (Jensen Huang summary) | + browser, Mwmbl; 3.6 s; 11 passages: forbes.com, businessinsider.com, en.wikipedia.org all naming Jensen Huang as CEO |
| how to fix 'ModuleNotFoundError: No module named pip' | Wikipedia only; 4.8 s. Top: **solar panels** ("Cells, modules, panels and systems") — wrong | + browser, Stack Exchange, GitHub issues, Mwmbl; 5.7 s. Top: stackabuse.com, sebhastian.com (the fix: `python -m ensurepip`), stackoverflow.com question + accepted answer |
| Rust borrow checker lifetime elision rules | nothing (Wikipedia: 0 results); **no passages** | + browser, Mwmbl, Stack Exchange; 8.5 s. Top: the three elision rules (softwarepatternslexicon.com), users.rust-lang.org, zetcode.com |
| latest news on the COP31 climate summit | Google News headlines (nytimes, nbcnews, climatechangenews) | + browser, Mwmbl, SearXNG; 11.5 s. Top: bloomberg.com, reuters.com result snippets (their pages refuse plain clients: 403 / 401), nytimes headline, theguardian.com article read |
| recent research on transformer attention efficiency papers | Wikipedia ("Flash attention"), Google News | + arXiv, OpenAlex, browser; 9.5 s. Top: arXiv abstracts, ieeexplore.ieee.org, researchgate.net |

The planner's cost: with a model client that accepts `max_tokens` it is one
≤ 60-token generation; otherwise it streams and stops when the JSON object
closes (closing the stream cancels the engine, `fish_native._stream`). It
does **not** share the main prompt's prefix, so the engine's prefix cache is
re-filled for the answer; on CPU with a small model expect a few hundred ms to
~2 s extra per non-trivial message. The pre-filter removes the cost for
greetings, thanks and code pastes.

## Privacy: what leaves the machine

For the website's privacy page, the exact list (2026-11-09):

- **The planned queries** (written by the model from your message and the
  last few turns; never your message history, no cookies, no account) go to:
  - **DuckDuckGo** — every web search, through the hidden embedded browser
    (private mode: no cookies or storage survive the helper process). DuckDuckGo
    sees the query and this computer's IP address, like any browser search.
  - **Bing (Microsoft)** — only when DuckDuckGo refused, failed or found
    nothing for that query.
  - **Mwmbl** (api.mwmbl.org) — every web search.
  - **Wikipedia** — every web search.
  - **Public SearXNG instances** (volunteer-run; each sees the queries it gets)
    — every web search, while one answers.
  - **DuckDuckGo Instant Answer API** — the *names* in the question (people,
    places, products), not the full question.
  - **Google News** — questions about current events.
  - **Stack Exchange, GitHub, Hacker News (Algolia)** — programming questions.
  - **arXiv, OpenAlex** — science / research questions.
  - **endoflife.date** — version / release questions (the product name).
  - **PyPI / npm / GitHub** — package or repository names you mention.
  - The RSS feed hosts (BBC, NPR, Guardian, Al Jazeera) see a feed download,
    not your query.
- **To result pages**: a normal GET of up to 4 pages per research run, with
  the IMURUN User-Agent. The engine never sends forms, never runs page
  scripts, refuses `localhost`, private, loopback and link-local literal
  addresses, and caps every download. (The embedded browser only loads the
  search engine's results page; result pages are read by the plain client.)
- **Locally**: plans are cached in memory for 5 minutes (64 entries), feeds
  for 5 minutes, the endoflife.date product list for a day, source failures for
  up to 10 minutes. Nothing is written to disk. `research.*` events go only to
  the local UI; `research.plan` events carry the queries, not your message.

## Terms

Feeds (Google News, publishers) are offered for personal, non-commercial
reading; IMURUN fetches them on the user's own machine, for that user. The
public APIs (Wikipedia, DuckDuckGo Instant Answers, Stack Exchange, GitHub,
Algolia HN, arXiv, OpenAlex, endoflife.date, Mwmbl) are used within their
published keyless limits, once per plan where they ask for restraint, with
the IMURUN User-Agent. The embedded browser loads a results page as the
user's own browser would, on the user's machine, for the user's question; it
never clicks, types, submits or solves a challenge (`BROWSER-SEARCH.md`).
Search engines' terms restrict automated use of their result pages; the
product owner decided (2026-11-09) to ship the helper on, with
`IMURUN_BROWSER_SEARCH=0` as the off switch. A hosted or commercial deployment
should revisit that.

## Integration

- `services/tools/builtin/web_search.py::WebSearchTool` is the research tool
  (`name="web_search"`; `query` required, optional `queries` up to 3).
- `services/tools/builtin/web_fetch.py::WebFetchTool` fetches through
  `engine.fetch_page` and cleans with `extract.py` (main content, published
  date, language). It now refuses local/private addresses.
- `services/tools/runner.py::ToolRunner`: `intents(prompt, history=, model=,
  publish=, turn_id=)` adds `web_search` when the plan says `needs_web`;
  weather / time / location keep their regex intents. `observe_full(...)`
  runs the plan through `WebSearchTool.run_plan`. Without a model argument
  the runner uses `planner_model` (if the server set it), else the heuristic.
- Events: `research.plan`, `research.search` (per source × query, or once
  per plan for `per_plan` sources: `ok`, `results`, `error`, `blocked`,
  `elapsed`; skipped sources appear in the result data with `skipped: true` and
  the `reason`, e.g. "paused after a failure: HTTP 429"), `research.fetch`
  (`url`, `status`, `ok`, `chars`, `error`, `lang`, `published`),
  `research.done` (`sources`, `passages`, `hits`, `errors`, `offline`,
  `blocked`, `elapsed`). New `provider` values in `sources[]`:
  `browser:duckduckgo`, `browser:bing`, `mwmbl`, `ddg_instant`,
  `stackexchange`, `github`, `hackernews`, `arxiv`, `openalex`, `endoflife`.
  A model-issued `web_search` also emits `web_search.start` / `web_search.done`;
  for the pre-fetch the loop emits those itself. All carry `turn_id`.
- No settings API is needed: there is nothing to configure. (The server's
  `search` settings section predates this and is no longer shown in the UI.)
- The loop hook that makes the pre-fetch model-planned is in `REQUESTS.md`.

## Tests

`tests/test_research_planner.py`, `test_research_extract.py` (fixtures in
`tests/fixtures/research/`), `test_research_rank.py`,
`test_research_sources.py` (fake HTTP layer; a fake helper process speaks the
browser protocol over real pipes), `test_research_keyless.py` (every keyless
API source against faked JSON/Atom, routing by topic, failure memory and
`Retry-After`, per-source time caps, Bing redirect unwrapping, the browser
source's per-plan queries and refusal memory, idle shutdown, a helper that
dies before `ready` degrading to the other sources),
`test_research_pipeline.py` (whole pipeline with the network faked; deadline;
offline / blocked / empty). `tests/conftest.py` sets `IMURUN_BROWSER_SEARCH=0`
so no test starts a real WebView. The live smoke test is skipped by default:

```
$env:IMURUN_NETWORK_TESTS="1"; python -m pytest -m network -s tests/test_research_pipeline.py
```
