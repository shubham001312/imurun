"""Stack Exchange API (keyless) for programming and tooling questions.

Two requests per plan (it runs once, with the first query):

1. ``/2.3/search/advanced`` - the most relevant questions on the chosen site
   (Stack Overflow; Ask Ubuntu or Super User when the question is about
   those), with their bodies;
2. ``/2.3/questions/{ids}/answers`` - the answers of the best two answered
   questions, by votes. The accepted answer wins, else the top-voted one.

The passage is the question title, the start of the question and the
answer, rendered from the API's HTML with ``extract.fragment_text``. Content
is CC BY-SA; the source chip links the question.

Without a key the API allows 300 requests per day per IP address and may
ask clients to wait (``backoff`` seconds in the body); both are honoured
through the failure memory. Measured 2026-11-09: 200 JSON in 0.4-0.8 s.
"""
from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from typing import Any

from ..extract import fragment_text
from ..net import with_params
from ..types import Hit, Plan
from . import ApiSource, SourceError

API = "https://api.stackexchange.com/2.3"
QUESTION_CHARS = 700
ANSWER_CHARS = 2600
QUESTIONS = 2
_UBUNTU = re.compile(r"\b(?:ubuntu|apt-get|apt install|snap|gnome|kubuntu|xubuntu)\b", re.IGNORECASE)
_SUPERUSER = re.compile(r"\b(?:windows 1[01]|windows explorer|registry|bios|uefi|bluetooth|wi-?fi|printer|"
                        r"excel|word|outlook|macos settings|finder)\b", re.IGNORECASE)
_PROGRAMMING = re.compile(r"\b(?:python|javascript|java|rust|c\+\+|c#|go|code|api|error|exception|compile|"
                          r"script|function|library|npm|pip)\b", re.IGNORECASE)


def site_for(plan: Plan, query: str) -> str:
    text = " ".join([plan.prompt or "", query])
    if _UBUNTU.search(text):
        return "askubuntu"
    if _SUPERUSER.search(text) and not _PROGRAMMING.search(text):
        return "superuser"
    return "stackoverflow"


def _date(value: Any) -> str:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _text(body: Any, limit: int) -> str:
    text = fragment_text(str(body or ""))
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


class StackExchangeSource(ApiSource):
    name = "stackexchange"
    per_plan = True
    topics = frozenset({"code"})
    timeout_cap = 5.0

    def _call(self, fetcher: Any, url: str, timeout: float) -> dict:
        data = self.get_json(fetcher, url, timeout)
        if not isinstance(data, dict):
            raise SourceError("stackexchange: unexpected answer")
        if data.get("error_id"):
            # throttle_violation (502), quota exhausted ... are refusals.
            self.backoff.refuse(str(data.get("error_name") or data.get("error_id")))
            raise SourceError(f"stackexchange: {data.get('error_message') or data.get('error_name')}",
                              blocked=True)
        if data.get("backoff"):
            self.backoff.refuse("asked to back off", float(data["backoff"]))
        if isinstance(data.get("quota_remaining"), int) and data["quota_remaining"] <= 2:
            self.backoff.refuse("daily quota used up", 3600.0)
        return data

    def search(self, query: str, plan: Plan, fetcher: Any, timeout: float) -> list[Hit]:
        site = site_for(plan, query)
        found = self._call(fetcher, with_params(f"{API}/search/advanced", {
            "order": "desc", "sort": "relevance", "q": query, "site": site, "pagesize": 5,
            "filter": "withbody"}), timeout)
        questions = [q for q in found.get("items") or [] if isinstance(q, dict) and q.get("question_id")]
        answered = [q for q in questions if int(q.get("answer_count") or 0) > 0][:QUESTIONS]
        best: dict[int, dict] = {}
        if answered:
            ids = ";".join(str(q["question_id"]) for q in answered)
            try:
                answers = self._call(fetcher, with_params(f"{API}/questions/{ids}/answers", {
                    "order": "desc", "sort": "votes", "site": site, "pagesize": 10, "filter": "withbody"}),
                    timeout)
            except SourceError:
                answers = {}
            for answer in answers.get("items") or []:
                if not isinstance(answer, dict):
                    continue
                qid = int(answer.get("question_id") or 0)
                kept = best.get(qid)
                if kept is None or (answer.get("is_accepted") and not kept.get("is_accepted")):
                    best[qid] = answer
        hits: list[Hit] = []
        for question in (answered or questions)[:QUESTIONS + 1]:
            qid = int(question["question_id"])
            title = html.unescape(" ".join(str(question.get("title") or "").split()))
            link = str(question.get("link") or f"https://{site}.com/questions/{qid}")
            if not title or not link.startswith("https://"):
                continue
            parts = [f"Question: {title}", _text(question.get("body"), QUESTION_CHARS)]
            answer = best.get(qid)
            published = _date(question.get("last_activity_date") or question.get("creation_date"))
            if answer is not None:
                label = "Accepted answer" if answer.get("is_accepted") else "Top answer"
                parts.append(f"{label} (score {answer.get('score', 0)}): {_text(answer.get('body'), ANSWER_CHARS)}")
                published = _date(answer.get("last_activity_date")) or published
            text = "\n\n".join(p for p in parts if p)
            hits.append(Hit(title=title, url=link, snippet=parts[1][:300], published=published,
                            source="stackexchange", query=query, rank=len(hits), text=text, fetchable=False))
        return hits
