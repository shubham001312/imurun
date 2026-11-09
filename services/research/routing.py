"""Which kind of question a plan is, so the right keyless sources run.

The planner decides *whether* to search and writes the queries; this module
only routes them. It reads the user's message, the planned queries and the
entities and returns a set of topics:

* ``general`` - always: whole-web sources (browser helper, Mwmbl, public
  SearXNG) plus Wikipedia and DuckDuckGo Instant Answers;
* ``code``    - programming, errors, tools, libraries: Stack Exchange,
  GitHub search, Hacker News;
* ``science`` - papers, studies, research results: arXiv, OpenAlex;
* ``news``    - the plan is ``live`` / ``recent``: the RSS sources;
* ``release`` - versions, releases, end of life: endoflife.date.

Plain regular expressions on purpose: routing has to be instant and must
not cost a second generation. A wrong guess costs one extra API request
(or one missing source), never a wrong answer: every source's passages are
still ranked against the question.
"""
from __future__ import annotations

import re

from .types import Plan

_CODE = re.compile(
    r"\b(?:python|pip|pypi|conda|npm|yarn|pnpm|node(?:\.?js)?|deno|bun|javascript|typescript|java|jvm|kotlin|"
    r"scala|rust|cargo|rustc|golang|c\+\+|c#|dotnet|\.net|ruby|rails|php|laravel|swift|xcode|objective-c|dart|"
    r"flutter|haskell|elixir|erlang|lua|perl|bash|zsh|powershell|shell script|sql|postgres(?:ql)?|mysql|sqlite|"
    r"mongodb|redis|docker|dockerfile|kubernetes|k8s|helm|terraform|ansible|git|github|gitlab|linux|ubuntu|"
    r"debian|fedora|arch linux|homebrew|wsl|vscode|vim|emacs|api|sdk|cli|library|framework|compiler|compile|"
    r"linker|runtime|exception|traceback|stack ?trace|segfault|segmentation fault|stderr|stdout|"
    r"struct|enum|trait|generic type|closure|lambda|python module|npm package|syntax error|"
    r"regex|async|await|thread|mutex|deadlock|memory leak|borrow checker|lifetime|ownership|pointer|"
    r"react|vue|angular|svelte|next\.js|django|flask|fastapi|pandas|numpy|pytorch|tensorflow|cuda|llvm|gcc|"
    r"clang|cmake|makefile|webpack|vite|eslint|json|yaml|toml|http|https|ssl|tls|oauth|websocket|grpc|"
    r"unit test|pytest|jest|devops|backend|frontend|programming|source code|coding|debug|debugging)\b",
    re.IGNORECASE,
)
# "ModuleNotFoundError", "NullPointerException", "E0502", "errno 13", a file name.
_CODE_SHAPE = re.compile(
    r"\b[A-Z][A-Za-z]+(?:Error|Exception)\b|\berrno\b|\bE\d{4}\b|\b\w+\.(?:py|js|ts|rs|go|java|cpp|h|cs|rb|"
    r"php|json|yaml|yml|toml|lock|sh|ps1)\b|`[^`]+`|\w+\(\)|::|->|=>",
)
_SCIENCE = re.compile(
    r"\b(?:paper|papers|preprint|arxiv|journal|peer[- ]reviewed|study|studies|meta[- ]analysis|systematic review|"
    r"clinical trial|randomi[sz]ed|cohort|hypothesis|theorem|conjecture|proof|lemma|physics|quantum|"
    r"chemistry|chemical|biology|biological|genome|genetic|protein|enzyme|neuroscience|neuron|"
    r"astronomy|astrophysics|cosmology|exoplanet|climate model|epidemiology|vaccine efficacy|"
    r"machine learning|deep learning|neural network|large language model|llm|transformer|reinforcement learning|"
    r"dataset|benchmark|state of the art|sota|citation|cited|doi|et al|research(?:ers)? (?:found|show))\b",
    re.IGNORECASE,
)
_RELEASE = re.compile(
    r"\b(?:latest|newest|current|stable|lts|recent)\b.{0,40}\b(?:version|release|releases|build)\b|"
    r"\b(?:version|release)\b.{0,30}\b(?:latest|newest|current|stable|lts)\b|"
    r"\b(?:end of life|eol|end-of-life|support(?:ed)? until|still supported|release date|released)\b",
    re.IGNORECASE,
)

TOPICS = ("general", "code", "science", "news", "release")


def plan_text(plan: Plan) -> str:
    return " ".join([plan.prompt or "", *plan.queries, *plan.entities])


def topics(plan: Plan) -> frozenset[str]:
    """The topics of this plan (always includes ``general``)."""
    text = plan_text(plan)
    found = {"general"}
    if _CODE.search(text) or _CODE_SHAPE.search(text):
        found.add("code")
    if _SCIENCE.search(text):
        found.add("science")
    if plan.freshness in ("live", "recent"):
        found.add("news")
    if _RELEASE.search(text):
        found.add("release")
    return frozenset(found)


__all__ = ["TOPICS", "plan_text", "topics"]
