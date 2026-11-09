# Research engine changelog

## 2026-11-09 — whole-web search with no key: embedded browser on, keyless APIs routed by topic

Symptom (owner): web search "is not that much good". Measured cause: without a
key there was no general web search at all. Public SearXNG answered 0 of 40
(2026-11-01) and 1 of 77 (today), and the browser helper was off. So the answer
came from Wikipedia and news RSS alone: "how to fix ModuleNotFoundError: No
module named pip" got a passage about solar panels, "Rust lifetime elision
rules" got nothing, and "latest Python release" got Wikipedia's 2008 history.
Owner's decisions today: no API keys for web search, ever; the
embedded-browser helper ships on.

Added:

- **Embedded browser on by default** (`IMURUN_BROWSER_SEARCH=0` = off). The
  window side (`window.py --search-helper`) already existed; verified live
  today (hidden, private mode): ready 1.3 s, DuckDuckGo 1.2-2.7 s, Bing 0.8-1.1 s.
  The source now runs once per plan (queries back to back in one 6 s budget),
  asks Bing only when DuckDuckGo refuses / fails / finds nothing, remembers a
  bot check per engine for 10 minutes, unwraps Bing `/ck/a?u=a1…` links (they
  were left as Bing redirect URLs), skips DuckDuckGo ad rows, and stops the
  helper after 10 idle minutes (`IMURUN_BROWSER_SEARCH_IDLE`).
- **Keyless API sources** (`sources/`): `mwmbl` (open web index), `ddg_instant`
  (DuckDuckGo Instant Answer API: entity abstracts), `stackexchange` (question +
  accepted answer), `github_search` (issues for errors, repositories for "a
  library for X"), `hackernews` (Algolia), `arxiv`, `openalex`, `endoflife`
  (release cycles). Probed and rejected, with reasons: Marginalia (shared key
  429), Reddit (403), Semantic Scholar (429), Wiby, Wikidata, Crossref, MDN,
  Wikinews, GDELT, Europe PMC, Lobsters (README "Why each source").
- **Routing** (`routing.py`): `general` / `code` / `science` / `news` / `release`
  from the message, queries and entities; `Source.topics` gates each source.
- **Failure memory for every API source** (`sources.Backoff`, `ApiSource`):
  403 / 429 / quota / Stack Exchange `backoff` park a source for 10 minutes or
  the server's `Retry-After` / GitHub reset (≤ 1 h); 3 errors in a row park it
  for 2 minutes. **Per-source time caps** (`Source.timeout_cap`, honoured by the
  engine): public SearXNG 4 s (it used to hold the search phase open for 6 s
  while being refused), APIs 4-6 s.
- **Ranking**: official docs (learn.microsoft.com, docs.rs, go.dev,
  kubernetes.io …) tier 1; Stack Exchange sites, arXiv, doi.org, OpenAlex,
  endoflife.date tier 0.75; fetch order weighs browser results 0.9, Mwmbl 0.7.
- `extract.fragment_text()` renders an API's HTML fragment (Stack Exchange
  bodies) with the page renderer.

Removed:

- The Settings > Web search key panel, its palette entry and the "set a key"
  hint in the blocked message (`tool.py::BLOCKED_HINT`). `sources/keyed.py`
  stays as an undocumented, environment-variable-only path for developers; the
  server's `search` settings section is no longer shown anywhere.

Measured end to end (before / after table in README "Budgets"): every test
question now gets on-topic passages from several independent sources; runs take
3.6-11.5 s (12 s deadline), up from 0.7-4.8 s, because pages are now found and read.

Tests: `tests/test_research_keyless.py` (new, 27 tests); `test_research_sources.py`
browser tests updated (default on, no-pywebview reason, window.py limits).
`tests/conftest.py` sets `IMURUN_BROWSER_SEARCH=0` so no test starts a WebView.

## 2026-11-02 — "what is" questions no longer trigger a search

Symptom (owner): every "what is ..." question ran a web search. Cause: the model
router (not the regex fallback) answers `web: true` to definitional questions -
measured on the 1B: "What is photosynthesis?" -> `{"web": true, "fresh": "live",
"q": ["photosynthesis", "Martin Luther King Jr." ...]}`; the heuristic says no for
all of them. Compact models already routed by heuristic, larger ones did not.
Fix: `planner.conceptual_question()` in the pre-filter - a definition /
how-it-works / why question ("what is X", "explain X", "how does X work") with no
live or recent wording, no proper noun beyond the first word, no quotes or digits
and no request to search is answered from knowledge, for every model size, and
never reaches the planner (saves the ~0.5-1 s routing generation too). The
planner prompt also names these as `web: false` examples. `tests/test_research_planner.py`.

## 2026-11-01 — research engine replaces keyword-triggered DuckDuckGo search

Product owner's requirement: free web search with no API key, accurate and
properly cleaned context, queries built by the AI from its understanding,
search decided by the AI and not by keyword matching.

Added (`services/research/`, see `README.md`):

- **Planner**: one ≤ 60-token routing generation with the same local model
  returns `{web, fresh, q[1-3], ent, why}`; forgiving JSON parsing; pre-filter
  for greetings / thanks / code pastes; per-message cache with in-flight
  de-duplication; heuristic fallback (the old regex rules) only when no model
  or no usable JSON.
- **Keyless sources**: public SearXNG (12 instances, rotation, 10-min failure
  memory, 2 in parallel), Wikipedia (moved from `web_search.py`; now also the
  best article's plain-text body), Google News RSS + public feeds, direct
  lookups (named sites, PyPI / npm / GitHub releases, official pages), and the
  embedded-browser helper protocol (client side; off by default). Keyed
  providers (Brave / Serper / Tavily / own SearXNG) moved to
  `sources/keyed.py` and are used first when configured.
- **Extractor**: readability-style main content, boilerplate removal,
  Markdown-ish rendering, dates, language.
- **Ranker**: canonical-URL and shingle dedupe, BM25 + entities + trust +
  freshness + present-tense cue, per-query coverage, token budget, numbered
  Context block with a citation instruction.
- **Engine**: 12 s total deadline, per-phase and per-request timeouts, never
  raises; events `research.plan/search/fetch/done`.

Changed:

- `tools/builtin/web_search.py`: `WebSearchTool` is the research tool
  (optional `queries` argument). Old helper names kept (`current_provider`
  now returns `research` when no key is set). The DuckDuckGo HTML /
  instant-answer paths are gone: DuckDuckGo answers this client with a
  bot-check page, and the engine does not disguise itself to get past it.
- `tools/builtin/web_fetch.py`: fetches through the research client and
  cleans with `extract.py` (main content, published date, language); refuses
  local / private addresses (a page or search result must not make the app
  call itself or the LAN). `extract_text` kept.
- `tools/runner.py`: `web_search` is added when the plan says `needs_web`
  (model-decided), and runs the plan through `run_plan`; weather / time /
  location keep their intents. `intents()` / `observe_full()` accept
  `history=, model=, publish=, turn_id=, stop=`; `observe()`'s return shape
  is unchanged. Research gets its own deadline (12 s + 1.5) instead of the
  10 s tool deadline.
- `tools/policy.py`: documented that the `web_search` patterns and
  `is_realtime` are the planner's fallback only.
- `tests/conftest.py`: registers the `network` marker; network tests are
  skipped unless `IMURUN_NETWORK_TESTS=1`.
- `tests/test_agentic_tools.py::test_blocked_search_is_a_failure_not_an_empty_answer`
  rewritten for the engine (same intent: a refusal is a failed result).

Ranking fixes found with the live smoke test:

- "current" / "latest" / "now" were stop words, so the passage "Narendra Modi,
  who is the current prime minister of India, serving since 26 May 2014"
  lost to an "Eligibility" section. They are content words now, and live
  questions reward present-tense cues.
- Wikipedia's REST `timestamp` (last edit) was used as the publication date,
  so encyclopaedic history outranked yesterday's news for "latest news on
  ISRO". Wikipedia passages carry no date now; live questions weight
  freshness 0.20.
- General news feeds matched "prime minister" in an unrelated country's
  story; feed items must now name one of the plan's entities.
- A forum thread kept only its longest post (widening stopped at the first
  ancestor that added nothing); every ancestor is considered now.

Measured (this machine, 2026-11-01): "who is the current prime minister of
India" → 3.1 s, top passage names the incumbent with the date; 0 of 40 public
SearXNG instances answered JSON (all 429 or an Anubis challenge).

Open: loop hook (R1), window helper (R2), server event sink (R3), event
docs (R4) — `REQUESTS.md`.
