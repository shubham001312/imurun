"""Connector tools are offered per service (docs/connectors/REQUESTS.md L5)."""
from __future__ import annotations

from dataclasses import dataclass, field

from services.tools.offer import SMALL_CATALOGUE, relevant_services, select_tools, service_of
from services.tools.protocol import tool_prompt


@dataclass
class _Tool:
    name: str
    category: str = "connector-read"
    description: str = "x"
    parameters: dict = field(default_factory=dict)

    def summary(self):
        return self.description


def _catalogue():
    tools = [_Tool("web_search", "network"), _Tool("file_read", "read"), _Tool("mcp_fake_echo", "mcp")]
    for service in ("gmail", "github", "slack", "vercel", "cloudflare", "supabase"):
        for verb in ("list", "read", "write"):
            tools.append(_Tool(f"{service}_{verb}", "connector-write" if verb == "write" else "connector-read"))
    return tools


def test_small_catalogue_is_never_pruned():
    tools = [_Tool("web_search", "network")] + [_Tool(f"gmail_{i}") for i in range(SMALL_CATALOGUE)]
    offered, hidden = select_tools(tools, "tell me a joke")
    assert offered == tools and hidden == []


def test_only_mentioned_services_are_offered_and_others_named():
    offered, hidden = select_tools(_catalogue(), "Any unread emails from Priya about the invoice?")
    names = {t.name for t in offered}
    assert {"gmail_list", "gmail_read", "gmail_write", "web_search", "file_read", "mcp_fake_echo"} <= names
    assert not any(n.startswith(("github_", "slack_", "vercel_", "cloudflare_", "supabase_")) for n in names)
    assert hidden == ["cloudflare", "github", "slack", "supabase", "vercel"]
    prompt = tool_prompt(offered, max_steps=4, hidden_services=hidden)
    assert "not listed here: cloudflare, github, slack, supabase, vercel" in prompt
    assert "gmail_read" in prompt and "github_read" not in prompt


def test_history_and_used_services_count():
    history = [{"role": "user", "content": "open an issue in the repo for the login bug"},
               {"role": "assistant", "content": "Done: issue #12."}]
    offered, hidden = select_tools(_catalogue(), "and assign it to me", history)
    assert any(t.name == "github_write" for t in offered) and "github" in {service_of(t) for t in offered}
    assert "github" not in hidden
    offered, hidden = select_tools(_catalogue(), "thanks", used_services=["slack"])
    assert any(t.name == "slack_write" for t in offered) and "slack" not in hidden


def test_hints_cover_the_owner_services_and_unknown_services_match_by_name():
    known = ("gmail", "github", "slack", "vercel", "render", "cloudflare", "supabase", "canva", "notion")
    assert relevant_services("purge the cache on the DNS zone", known) == {"cloudflare"}
    assert relevant_services("deploy the preview url", known) == {"vercel", "render"}
    assert relevant_services("add a row to the table in supabase", known) == {"supabase"}
    assert relevant_services("make a poster in canva", known) == {"canva"}
    assert relevant_services("sync my Notion page", known) == {"notion"}
    assert relevant_services("what is 2+2", known) == set()


def test_nothing_hidden_when_every_service_is_mentioned():
    text = "email github slack vercel cloudflare supabase"
    offered, hidden = select_tools(_catalogue(), text)
    assert hidden == [] and len(offered) == len(_catalogue())
