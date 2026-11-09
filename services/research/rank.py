"""Rank and condense: from hits and pages to the Context block the model reads.

1. **Dedupe** hits by canonical URL (scheme, ``www.``, tracking parameters,
   fragments, trailing slashes and AMP variants removed) and pages by
   near-duplicate text (5-word shingles, Jaccard >= 0.8).
2. **Chunk** every page (and every snippet) into passages of about
   ``CHUNK_CHARS`` characters on paragraph boundaries, each prefixed by its
   nearest heading.
3. **Score** each passage:
   ``0.55 * BM25 (best over the planned queries, normalised)``
   ``+ 0.15 * entity coverage`` ``+ 0.15 * domain trust tier``
   ``+ 0.10 * freshness`` ``+ 0.05 * date match``; snippet-only passages
   (no page fetched) count 0.8x.
4. **Select** under a token budget (characters / 3.5, the same estimate as
   ``response/context.py``): first the best passage for each query, then the
   best of the rest, at most two passages per source, near-duplicate
   passages dropped (Jaccard >= 0.6).
5. **Format** numbered sources ``[n] title - domain - date``, the passages
   tagged ``[n]``, and one line telling the model to cite ``[n]``.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .types import Hit, Page, Passage, Plan, domain_of

CHARS_PER_TOKEN = 3.5
CHUNK_CHARS = 700
MAX_PASSAGE_CHARS = 900
PER_SOURCE = 2
TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|dclid|msclkid|mc_[ce]id|ref|ref_src|cmpid|ito|ocid|"
                      r"igshid|spm|_ga|guccounter|guce_\w+|outputType|amp)$", re.IGNORECASE)
_WORD = re.compile(r"\w+", re.UNICODE)
STOP = set("""a an the of in on at to for from by with and or but is are was were be been being it its
this that these those what which who whom whose when where why how do does did done have has had can
could will would shall should may might must about into over after before than then there here as
not no yes i you he she they we me my your his her their our us them so if also just very more most
please tell me know find search look up""".split())
# "current", "latest", "now" are *not* stop words: for a live question the
# passage that says "who is the current prime minister" is the answer.
PRESENT_CUE = re.compile(r"\b(?:current(?:ly)?|incumbent|present|presently|sitting|serving since|since \d{4}|"
                         r"as of \d{4}|as of (?:today|now)|is now|took office|sworn[- ]in|latest|today)\b",
                         re.IGNORECASE)
MAX_HEADLINES = 3

# Domain trust tiers. 1.0: official, reference, wire services; 0.75: major
# newsrooms and Q&A with editorial control; default 0.45; 0.15: social media
# and user-generated aggregators.
TIER1_SUFFIX = (".gov", ".mil", ".edu", ".int")
TIER1_PATTERN = re.compile(r"(?:^|\.)(?:gov|gouv|gob|go|govt|nic|ac|edu|mil)\.[a-z]{2}$")
TIER1 = {"wikipedia.org", "britannica.com", "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com",
         "python.org", "docs.python.org", "developer.mozilla.org", "github.com", "who.int", "un.org",
         "europa.eu", "nature.com", "science.org", "nasa.gov", "pypi.org", "npmjs.com", "nodejs.org",
         "rust-lang.org", "kernel.org", "w3.org", "ietf.org", "imf.org", "worldbank.org", "pmindia.gov.in",
         "gov.uk", "whitehouse.gov", "nih.gov", "cdc.gov"}
TIER2 = {"nytimes.com", "theguardian.com", "washingtonpost.com", "aljazeera.com", "npr.org", "cnn.com",
         "bloomberg.com", "ft.com", "economist.com", "wsj.com", "thehindu.com", "hindustantimes.com",
         "indianexpress.com", "timesofindia.indiatimes.com", "ndtv.com", "stackoverflow.com",
         "stackexchange.com", "arstechnica.com", "theverge.com", "techcrunch.com", "nature.com",
         "espn.com", "cricbuzz.com", "espncricinfo.com", "cnbc.com", "forbes.com", "dw.com", "france24.com",
         "abc.net.au", "cbc.ca", "lemonde.fr", "spiegel.de", "nikkei.com", "scmp.com", "medium.com"}
LOW = {"pinterest.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
       "quora.com", "answers.com", "scribd.com", "slideshare.net", "linkedin.com"}


# ---------------------------------------------------------------- utilities
def canonical_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m.") or host.startswith("amp."):
        host = host.split(".", 1)[1]
    path = re.sub(r"/(?:amp|amp\.html)/?$", "/", parsed.path or "/")
    path = re.sub(r"/index\.(?:html?|php)$", "/", path)
    path = path.rstrip("/") or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
                             if not TRACKING.match(k)))
    return urlunparse(("https", host, path, "", query, ""))


def tokens(text: str) -> list[str]:
    out = []
    for word in _WORD.findall((text or "").lower()):
        if word in STOP or (len(word) < 2 and not word.isdigit()):
            continue
        if len(word) > 4 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        out.append(word)
    return out


def shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").lower())
    if len(words) < size:
        return {tuple(words)} if words else set()
    return {tuple(words[i:i + size]) for i in range(len(words) - size + 1)}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def trust(domain: str) -> float:
    domain = (domain or "").lower()
    parts = domain.split(".")
    candidates = {".".join(parts[i:]) for i in range(len(parts) - 1)}
    if candidates & LOW:
        return 0.15
    if domain.endswith(TIER1_SUFFIX) or TIER1_PATTERN.search(domain) or candidates & TIER1:
        return 1.0
    if candidates & TIER2:
        return 0.75
    return 0.45


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            match = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
            if not match:
                return None
            parsed = datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def freshness_score(published: str, freshness: str, now: datetime | None = None) -> float:
    if freshness == "any":
        return 0.5
    now = now or datetime.now(timezone.utc)
    date = parse_date(published)
    if date is None:
        return 0.3 if freshness == "live" else 0.4
    age = max(0.0, (now - date).total_seconds() / 86400)
    if freshness == "live":
        return 1.0 if age <= 2 else 0.8 if age <= 7 else 0.6 if age <= 30 else 0.3 if age <= 365 else 0.0
    return 1.0 if age <= 365 else 0.5 if age <= 730 else 0.1


def short_date(published: str) -> str:
    date = parse_date(published)
    return date.date().isoformat() if date else ""


# ---------------------------------------------------------------- chunking
def chunk(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Passages of about ``size`` characters, each starting with its heading."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n(?=#)", text or "") if b.strip()]
    out: list[str] = []
    heading = ""
    current: list[str] = []
    length = 0

    def flush() -> None:
        nonlocal current, length
        if current:
            body = "\n".join(current)
            out.append((heading + "\n" + body) if heading and not body.startswith("#") else body)
        current, length = [], 0

    for block in blocks:
        if block.startswith("#") and "\n" not in block:
            flush()
            heading = block
            continue
        if len(block) > size * 1.5:
            flush()
            sentences = re.split(r"(?<=[.!?])\s+", block)
            piece = ""
            for sentence in sentences:
                if len(piece) + len(sentence) > size and piece:
                    current, length = [piece.strip()], len(piece)
                    flush()
                    piece = ""
                piece += sentence + " "
            if piece.strip():
                current, length = [piece.strip()], len(piece)
                flush()
            continue
        if length + len(block) > size and current:
            flush()
        current.append(block)
        length += len(block)
    flush()
    return out


# ---------------------------------------------------------------- BM25
class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75) -> None:
        self.docs = docs
        self.k1, self.b = k1, b
        self.avg = (sum(len(d) for d in docs) / len(docs)) if docs else 0.0
        df: Counter = Counter()
        for doc in docs:
            df.update(set(doc))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        self.tf = [Counter(doc) for doc in docs]

    def score(self, query: list[str], index: int) -> float:
        tf = self.tf[index]
        length = len(self.docs[index]) or 1
        total = 0.0
        for term in set(query):
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = self.idf.get(term, 0.0)
            total += idf * freq * (self.k1 + 1) / (freq + self.k1 * (1 - self.b + self.b * length / (self.avg or 1)))
        return total


# ---------------------------------------------------------------- pipeline
def dedupe_hits(hits: list[Hit]) -> list[Hit]:
    """One hit per canonical URL; the first keeps its place, snippets merge."""
    by_url: dict[str, Hit] = {}
    order: list[str] = []
    for hit in hits:
        key = canonical_url(hit.url)
        if key in by_url:
            kept = by_url[key]
            if len(hit.snippet) > len(kept.snippet):
                kept.snippet = hit.snippet
            if len(hit.text) > len(kept.text):
                kept.text = hit.text
            kept.published = kept.published or hit.published
            continue
        by_url[key] = hit
        order.append(key)
    return [by_url[k] for k in order]


SOURCE_WEIGHT = {"direct": 1.0, "wikipedia": 0.9, "pypi": 1.0, "npm": 1.0, "github": 1.0}


def fetch_order(hits: list[Hit], plan: Plan) -> list[Hit]:
    """Fetchable hits, best first, one per domain."""
    query_terms = set(tokens(" ".join(plan.queries + plan.entities)))
    scored = []
    for hit in hits:
        if not hit.fetchable or hit.text:
            continue
        overlap = len(query_terms & set(tokens(hit.title + " " + hit.snippet))) / (len(query_terms) or 1)
        score = (SOURCE_WEIGHT.get(hit.source, 0.7) + trust(hit.domain) + overlap
                 + 1.0 / (1 + hit.rank))
        scored.append((score, hit))
    scored.sort(key=lambda pair: -pair[0])
    out, domains = [], set()
    for _, hit in scored:
        if hit.domain in domains:
            continue
        domains.add(hit.domain)
        out.append(hit)
    return out


def passages_for(hits: list[Hit], pages: dict[str, Page]) -> list[Passage]:
    """Every chunk of every page, plus snippet passages for unfetched hits."""
    out: list[Passage] = []
    page_shingles: list[set] = []
    for hit in hits:
        page = pages.get(hit.url)
        body = page.text if page is not None and page.text else hit.text
        published = (page.published if page is not None and page.published else "") or hit.published
        title = (page.title if page is not None and page.title else "") or hit.title
        if body and len(body.strip()) >= 20:
            fingerprint = shingles(body[:3000])
            if any(jaccard(fingerprint, other) >= 0.8 for other in page_shingles):
                continue
            page_shingles.append(fingerprint)
            for piece in chunk(body):
                out.append(Passage(url=hit.url, title=title, domain=hit.domain, text=piece,
                                   provider=hit.source, published=published, query=hit.query))
            continue
        snippet = " ".join((hit.snippet or "").split())
        if snippet and len(snippet) >= 20:
            text = snippet if hit.title.lower() in snippet.lower() else f"{hit.title}: {snippet}"
            out.append(Passage(url=hit.url, title=title, domain=hit.domain, text=text, provider=hit.source,
                               published=published, query=hit.query, snippet_only=True))
        elif hit.title and hit.source in ("google_news", "bbc", "npr", "guardian", "aljazeera"):
            out.append(Passage(url=hit.url, title=title, domain=hit.domain, text=hit.title, provider=hit.source,
                               published=published, query=hit.query, snippet_only=True))
    return out


def score_passages(passages: list[Passage], plan: Plan, now: datetime | None = None) -> dict[int, dict[str, float]]:
    """Score in place; returns per-query BM25 (normalised) for each passage."""
    if not passages:
        return {}
    docs = [tokens(p.title + " " + p.text) for p in passages]
    bm25 = BM25(docs)
    queries = [tokens(q) for q in (plan.queries or [])] or [tokens(" ".join(plan.entities))]
    per_query: dict[int, dict[str, float]] = {i: {} for i in range(len(passages))}
    for q_index, query in enumerate(queries):
        raw = [bm25.score(query, i) for i in range(len(passages))]
        best = max(raw) or 1.0
        for i, value in enumerate(raw):
            per_query[i][str(q_index)] = value / best
    entity_terms = [set(tokens(e)) for e in plan.entities if tokens(e)]
    year = str((now or datetime.now(timezone.utc)).year)
    for i, passage in enumerate(passages):
        relevance = max(per_query[i].values()) if per_query[i] else 0.0
        words = set(docs[i])
        coverage = (sum(1 for e in entity_terms if e <= words) / len(entity_terms)) if entity_terms else 0.5
        dated = 1.0 if plan.freshness != "any" and year in passage.text else 0.0
        present = 1.0 if plan.freshness == "live" and PRESENT_CUE.search(passage.text) else 0.0
        fresh = freshness_score(passage.published, plan.freshness, now)
        if plan.freshness == "live":
            # Live questions: when it was published matters as much as trust.
            score = (0.45 * relevance + 0.10 * coverage + 0.15 * trust(passage.domain)
                     + 0.20 * fresh + 0.05 * dated + 0.10 * present)
        else:
            score = (0.55 * relevance + 0.15 * coverage + 0.15 * trust(passage.domain)
                     + 0.10 * fresh + 0.05 * dated)
        if passage.snippet_only:
            score *= 0.8
        if relevance == 0.0:
            score *= 0.3  # nothing to do with the question
        passage.score = round(score, 4)
    return per_query


def select(passages: list[Passage], plan: Plan, budget_tokens: int,
           per_query: dict[int, dict[str, float]] | None = None) -> list[Passage]:
    budget = int(budget_tokens * CHARS_PER_TOKEN)
    order = sorted(range(len(passages)), key=lambda i: -passages[i].score)
    chosen: list[int] = []
    used = 0
    per_source: Counter = Counter()
    fingerprints: list[set] = []
    headlines = 0

    def take(i: int) -> bool:
        nonlocal used, headlines
        passage = passages[i]
        if i in chosen or per_source[passage.url] >= PER_SOURCE:
            return False
        headline = passage.snippet_only and len(passage.text) < 160
        if headline and headlines >= MAX_HEADLINES:
            return False
        text = passage.text[:MAX_PASSAGE_CHARS]
        cost = len(text) + 12
        if used + cost > budget and chosen:
            return False
        fingerprint = shingles(text)
        if any(jaccard(fingerprint, other) >= 0.6 for other in fingerprints):
            return False
        chosen.append(i)
        fingerprints.append(fingerprint)
        per_source[passage.url] += 1
        used += cost
        headlines += 1 if headline else 0
        return True

    # 1. The best passage for each planned query.
    if per_query:
        for q_index in range(len(plan.queries)):
            key = str(q_index)
            ranked = sorted(order, key=lambda i: -(per_query[i].get(key, 0.0) * 0.6 + passages[i].score * 0.4))
            for i in ranked[:5]:
                if per_query[i].get(key, 0.0) > 0 and take(i):
                    break
    # 2. The best of the rest, while it is still clearly relevant.
    floor = max(0.2, passages[order[0]].score * 0.45) if order else 0.2
    for i in order:
        if passages[i].score < floor:
            break
        take(i)
    return sorted((passages[i] for i in chosen), key=lambda p: -p.score)


def build_context(plan: Plan, hits: list[Hit], pages: dict[str, Page], budget_tokens: int,
                  now: datetime | None = None) -> tuple[str, list[dict[str, str]], list[Passage]]:
    """The Context block, the source chips, and the passages used."""
    hits = dedupe_hits(hits)
    passages = passages_for(hits, pages)
    per_query = score_passages(passages, plan, now)
    chosen = select(passages, plan, budget_tokens, per_query)
    if not chosen:
        return "", [], []
    numbers: dict[str, int] = {}
    sources: list[dict[str, str]] = []
    source_lines: list[str] = []
    for passage in chosen:
        if passage.url in numbers:
            continue
        numbers[passage.url] = len(numbers) + 1
        date = short_date(passage.published)
        source_lines.append(f"[{numbers[passage.url]}] {passage.title[:120]} — {passage.domain}"
                            + (f" — {date}" if date else ""))
        sources.append({"title": passage.title[:120], "url": passage.url, "domain": passage.domain,
                        "provider": passage.provider,
                        "snippet": " ".join(passage.text.split())[:300]})
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"Web research for: {'; '.join(plan.queries) or '-'} (searched {when})", "Sources:"]
    lines.extend(source_lines)
    lines.append("Passages:")
    for passage in chosen:
        text = passage.text[:MAX_PASSAGE_CHARS].strip()
        if len(passage.text) > MAX_PASSAGE_CHARS:
            text = text.rsplit(" ", 1)[0] + " ..."
        lines.append(f"[{numbers[passage.url]}] {text}")
    lines.append("Cite the passages you use as [n]. For facts about the present use only these passages; "
                 "if they do not answer the question, say so in one short sentence.")
    return "\n".join(lines), sources, chosen
