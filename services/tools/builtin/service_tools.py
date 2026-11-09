"""Model tools for GitHub, Slack, Vercel, Render, Cloudflare, Supabase and Canva.

Reads are ``connector-read``; every write is ``connector-write`` with level
``always`` (an approval card in every mode). Output is compact: one line per
item with the id the next tool needs. See docs/connectors/<SERVICE>.md.
"""
from __future__ import annotations

import base64
import json
from typing import Any

from ..base import Tool
from .connector_tools import ServiceContext, ServiceTool, cut

S = {"type": "string"}
I = {"type": "integer"}


def _props(**fields) -> dict:
    return {k: (dict(v[0], description=v[1]) if isinstance(v, tuple) else v) for k, v in fields.items()}


REPO = (S, "owner/name, e.g. octocat/hello-world")
LIMIT = (I, "How many items (default 20)")


# ------------------------------------------------------------------ GitHub
class GitHubRepos(ServiceTool):
    name = "github_repos"
    description = "List the user's GitHub repositories (most recently updated first) with stars, language and visibility."
    parameters = {"type": "object", "properties": _props(limit=LIMIT), "required": []}

    def _run(self, args):
        items = self.ctx.call("list_repos", {"limit": args.get("limit")}).get("items") or []
        lines = [f"{len(items)} repositories:"]
        for repo in items:
            flag = "private" if repo.get("private") else "public"
            lines.append(f"- {repo.get('full_name')} ({flag}, {repo.get('language') or '-'}, "
                         f"★{repo.get('stargazers_count', 0)}, {repo.get('open_issues_count', 0)} open) "
                         f"{cut(repo.get('description'), 100)}")
        return self.ok("\n".join(lines), {"repos": [r.get("full_name") for r in items]})


class GitHubIssues(ServiceTool):
    name = "github_issues"
    description = "List issues in a GitHub repository (open by default; pull requests are left out)."
    parameters = {"type": "object", "properties": _props(
        repo=REPO, state=({"type": "string", "enum": ["open", "closed", "all"]}, "open, closed or all"),
        limit=LIMIT), "required": ["repo"]}

    def _run(self, args):
        items = self.ctx.call("list_issues", args).get("items") or []
        issues = [i for i in items if "pull_request" not in i]
        lines = [f"{len(issues)} issue(s) in {args['repo']}:"]
        for issue in issues:
            lines.append(f"- #{issue.get('number')} [{issue.get('state')}] {cut(issue.get('title'), 120)} "
                         f"(by {(issue.get('user') or {}).get('login', '?')}, {issue.get('comments', 0)} comments)")
        return self.ok("\n".join(lines), {"numbers": [i.get("number") for i in issues]})


class GitHubIssue(ServiceTool):
    name = "github_issue"
    description = "Read one GitHub issue (or pull request conversation) with its latest comments."
    parameters = {"type": "object", "properties": _props(repo=REPO, number=(I, "Issue number")),
                  "required": ["repo", "number"]}

    def _run(self, args):
        result = self.ctx.call("get_issue", args)
        issue, comments = result.get("issue") or {}, result.get("comments") or []
        lines = [f"#{issue.get('number')} {issue.get('title')} [{issue.get('state')}] by "
                 f"{(issue.get('user') or {}).get('login', '?')}", self.untrusted(),
                 cut(issue.get("body"), 3000) or "(no description)"]
        for comment in comments[-10:]:
            lines.append(f"-- {(comment.get('user') or {}).get('login', '?')}: {cut(comment.get('body'), 600)}")
        return self.ok("\n".join(lines), {"number": issue.get("number"), "comments": len(comments)})


class GitHubPulls(ServiceTool):
    name = "github_pulls"
    description = "List pull requests in a GitHub repository (open by default)."
    parameters = GitHubIssues.parameters

    def _run(self, args):
        items = self.ctx.call("list_pulls", args).get("items") or []
        lines = [f"{len(items)} pull request(s) in {args['repo']}:"]
        for pr in items:
            lines.append(f"- #{pr.get('number')} [{pr.get('state')}{', draft' if pr.get('draft') else ''}] "
                         f"{cut(pr.get('title'), 120)} ({(pr.get('head') or {}).get('ref', '?')} -> "
                         f"{(pr.get('base') or {}).get('ref', '?')}, by {(pr.get('user') or {}).get('login', '?')})")
        return self.ok("\n".join(lines), {"numbers": [p.get("number") for p in items]})


class GitHubPull(ServiceTool):
    name = "github_pull"
    description = "Read one GitHub pull request: description, status and the files it changes."
    parameters = GitHubIssue.parameters

    def _run(self, args):
        result = self.ctx.call("get_pull", args)
        pr, files = result.get("pull") or {}, result.get("files") or []
        lines = [f"PR #{pr.get('number')} {pr.get('title')} [{pr.get('state')}"
                 f"{', merged' if pr.get('merged') else ''}] {(pr.get('head') or {}).get('ref')} -> "
                 f"{(pr.get('base') or {}).get('ref')}", self.untrusted(), cut(pr.get("body"), 3000) or "(no description)",
                 f"{len(files)} file(s) changed:"]
        lines += [f"- {f.get('filename')} (+{f.get('additions', 0)} -{f.get('deletions', 0)}, {f.get('status')})"
                  for f in files[:50]]
        return self.ok("\n".join(lines), {"number": pr.get("number"), "files": [f.get("filename") for f in files]})


class GitHubFile(ServiceTool):
    name = "github_file"
    description = "Read a text file from a GitHub repository (default branch unless ref is given)."
    parameters = {"type": "object", "properties": _props(
        repo=REPO, path=(S, "File path in the repository, e.g. src/main.py"),
        ref=(S, "Optional branch, tag or commit"), max_chars=(I, "Characters to return (default 8000)")),
        "required": ["repo", "path"]}

    def _run(self, args):
        result = self.ctx.call("read_file", args)
        if isinstance(result, list):
            names = ", ".join(f"{i.get('name')}{'/' if i.get('type') == 'dir' else ''}" for i in result[:200])
            return self.ok(f"{args['path']} is a folder: {names}", {"entries": len(result)})
        raw = base64.b64decode(str(result.get("content") or "")) if result.get("encoding") == "base64" else b""
        if b"\x00" in raw[:4000]:
            return self.ok(f"{args['path']} is a binary file ({result.get('size')} bytes); not shown.")
        text = raw.decode("utf-8", errors="replace")
        limit = max(500, min(int(args.get("max_chars") or 8000), 40000))
        note = f"\n... [file cut at {limit} of {len(text)} characters]" if len(text) > limit else ""
        return self.ok(f"{args['repo']}:{args['path']} ({result.get('size')} bytes)\n{text[:limit]}{note}",
                       {"sha": result.get("sha"), "size": result.get("size")})


class GitHubCreateIssue(ServiceTool):
    name = "github_create_issue"
    level = "always"
    description = "Open a new issue in a GitHub repository. The user always approves first."
    parameters = {"type": "object", "properties": _props(repo=REPO, title=(S, "Issue title"),
                                                         body=(S, "Issue text (Markdown)")),
                  "required": ["repo", "title"]}

    def describe(self, args):
        return f"opens an issue in {args.get('repo')}: \"{cut(args.get('title'), 120)}\""

    def _run(self, args):
        issue = self.ctx.call("create_issue", args)
        return self.ok(f"Opened #{issue.get('number')}: {issue.get('html_url')}", {"number": issue.get("number")})


class GitHubComment(ServiceTool):
    name = "github_comment"
    level = "always"
    description = "Comment on a GitHub issue or pull request. The user always approves first."
    parameters = {"type": "object", "properties": _props(repo=REPO, number=(I, "Issue or PR number"),
                                                         body=(S, "Comment text (Markdown)")),
                  "required": ["repo", "number", "body"]}

    def describe(self, args):
        return f"posts a public comment on {args.get('repo')}#{args.get('number')}"

    def _run(self, args):
        comment = self.ctx.call("comment", args)
        return self.ok(f"Commented: {comment.get('html_url')}", {"id": comment.get("id")})


# ------------------------------------------------------------------- Slack
class SlackChannels(ServiceTool):
    name = "slack_channels"
    description = "List Slack channels the IMURUN app can see (name, id, members, whether it is a member)."
    parameters = {"type": "object", "properties": _props(limit=LIMIT), "required": []}

    def _run(self, args):
        channels = self.ctx.call("list_channels", {"limit": args.get("limit") or 100}).get("channels") or []
        lines = [f"{len(channels)} channel(s):"]
        for c in channels:
            member = "member" if c.get("is_member") else "not a member (invite the app to read it)"
            lines.append(f"- #{c.get('name')} [{c.get('id')}] {c.get('num_members', '?')} people, {member}")
        return self.ok("\n".join(lines), {"channels": [c.get("id") for c in channels]})


class SlackHistory(ServiceTool):
    name = "slack_history"
    description = "Read the latest messages in a Slack channel (use the channel id from slack_channels)."
    parameters = {"type": "object", "properties": _props(channel=(S, "Channel id, e.g. C0123ABC"), limit=LIMIT),
                  "required": ["channel"]}

    def _run(self, args):
        messages = self.ctx.call("get_history", args).get("messages") or []
        lines = [f"{len(messages)} message(s), newest first:", self.untrusted()]
        for m in messages:
            lines.append(f"- [{m.get('ts')}] {m.get('user') or m.get('username') or m.get('bot_id') or '?'}: "
                         f"{cut(m.get('text'), 400)}")
        return self.ok("\n".join(lines), {"count": len(messages)})


class SlackPost(ServiceTool):
    name = "slack_post"
    level = "always"
    description = "Post a message to a Slack channel (optionally in a thread). The user always approves first."
    parameters = {"type": "object", "properties": _props(channel=(S, "Channel id"), text=(S, "Message text"),
                                                         thread_ts=(S, "Optional thread timestamp to reply in")),
                  "required": ["channel", "text"]}

    def describe(self, args):
        return f"posts to Slack channel {args.get('channel')}: \"{cut(args.get('text'), 160)}\""

    def _run(self, args):
        result = self.ctx.call("post_message", args)
        return self.ok(f"Posted to {result.get('channel')} (ts {result.get('ts')}).", {"ts": result.get("ts")})


# ------------------------------------------------------------------ Vercel
class VercelProjects(ServiceTool):
    name = "vercel_projects"
    description = "List the user's Vercel projects (name, id, framework, last update)."
    parameters = {"type": "object", "properties": _props(limit=LIMIT, team_id=(S, "Optional team id")),
                  "required": []}

    def _run(self, args):
        projects = self.ctx.call("list_projects", args).get("projects") or []
        lines = [f"{len(projects)} project(s):"]
        lines += [f"- {p.get('name')} [{p.get('id')}] {p.get('framework') or '-'}" for p in projects]
        return self.ok("\n".join(lines), {"projects": [p.get("id") for p in projects]})


class VercelDeployments(ServiceTool):
    name = "vercel_deployments"
    description = "List recent Vercel deployments, optionally for one project (state, target, url, id)."
    parameters = {"type": "object", "properties": _props(project=(S, "Optional project id"), limit=LIMIT,
                                                         team_id=(S, "Optional team id")), "required": []}

    def _run(self, args):
        items = self.ctx.call("list_deployments", args).get("deployments") or []
        lines = [f"{len(items)} deployment(s):"]
        lines += [f"- {d.get('uid')} {d.get('name')} [{d.get('state') or d.get('readyState')}, "
                  f"{d.get('target') or 'preview'}] https://{d.get('url')}" for d in items]
        return self.ok("\n".join(lines), {"deployments": [d.get("uid") for d in items]})


class VercelLogs(ServiceTool):
    name = "vercel_logs"
    description = "Read the build log of one Vercel deployment (use the id from vercel_deployments)."
    parameters = {"type": "object", "properties": _props(deployment=(S, "Deployment id, e.g. dpl_..."),
                                                         limit=(I, "Lines (default 100)"), team_id=(S, "Optional team id")),
                  "required": ["deployment"]}

    def _run(self, args):
        events = self.ctx.call("deployment_events", args).get("events") or []
        lines = [f"{len(events)} log line(s):"]
        for e in events if isinstance(events, list) else []:
            text = e.get("text") or (e.get("payload") or {}).get("text") or ""
            if text:
                lines.append(cut(text, 300))
        return self.ok("\n".join(lines[-200:]), {"lines": len(events) if isinstance(events, list) else 0})


class VercelRedeploy(ServiceTool):
    name = "vercel_redeploy"
    level = "always"
    description = "Redeploy an existing Vercel deployment (preview by default). The user always approves first."
    parameters = {"type": "object", "properties": _props(
        deployment=(S, "Deployment id to redeploy"), name=(S, "Project name"),
        target=({"type": "string", "enum": ["preview", "production"]}, "preview or production"),
        team_id=(S, "Optional team id")), "required": ["deployment", "name"]}

    def describe(self, args):
        return f"redeploys {args.get('name')} to {args.get('target') or 'preview'} on Vercel"

    def _run(self, args):
        d = self.ctx.call("redeploy", args)
        return self.ok(f"Redeploy started: {d.get('id')} https://{d.get('url')}", {"id": d.get("id")})


# ------------------------------------------------------------------ Render
def _render_items(items, key):
    return [(i.get(key) if isinstance(i, dict) and key in i else i) for i in items or []]


class RenderServices(ServiceTool):
    name = "render_services"
    description = "List the user's Render services (name, id, type, status)."
    parameters = {"type": "object", "properties": _props(limit=LIMIT), "required": []}

    def _run(self, args):
        services = _render_items(self.ctx.call("list_services", args).get("items"), "service")
        lines = [f"{len(services)} service(s):"]
        lines += [f"- {s.get('name')} [{s.get('id')}] {s.get('type')} "
                  f"{'suspended' if s.get('suspended') == 'suspended' else 'active'}" for s in services]
        return self.ok("\n".join(lines), {"services": [s.get("id") for s in services]})


class RenderDeploys(ServiceTool):
    name = "render_deploys"
    description = "List recent deploys of one Render service (status, commit, time)."
    parameters = {"type": "object", "properties": _props(service=(S, "Service id, e.g. srv-..."), limit=LIMIT),
                  "required": ["service"]}

    def _run(self, args):
        deploys = _render_items(self.ctx.call("list_deploys", args).get("items"), "deploy")
        lines = [f"{len(deploys)} deploy(s):"]
        lines += [f"- {d.get('id')} [{d.get('status')}] {cut((d.get('commit') or {}).get('message'), 80)} "
                  f"{d.get('finishedAt') or d.get('createdAt') or ''}" for d in deploys]
        return self.ok("\n".join(lines), {"deploys": [d.get("id") for d in deploys]})


class RenderLogs(ServiceTool):
    name = "render_logs"
    description = "Read the latest log lines of one Render service."
    parameters = {"type": "object", "properties": _props(service=(S, "Service id"), limit=(I, "Lines (default 50)")),
                  "required": ["service"]}

    def _run(self, args):
        logs = self.ctx.call("logs", args).get("logs") or []
        lines = [f"{len(logs)} log line(s):"]
        lines += [f"{l.get('timestamp', '')} {cut(l.get('message'), 300)}" for l in logs]
        return self.ok("\n".join(lines), {"lines": len(logs)})


class RenderDeploy(ServiceTool):
    name = "render_deploy"
    level = "always"
    description = "Start a new deploy of a Render service. The user always approves first."
    parameters = {"type": "object", "properties": _props(service=(S, "Service id"),
                                                         clear_cache=({"type": "boolean"}, "Clear the build cache")),
                  "required": ["service"]}

    def describe(self, args):
        return f"deploys Render service {args.get('service')}"

    def _run(self, args):
        d = self.ctx.call("trigger_deploy", args)
        return self.ok(f"Deploy {d.get('id')} started ({d.get('status')}).", {"id": d.get("id")})


class RenderRestart(ServiceTool):
    name = "render_restart"
    level = "always"
    description = "Restart a Render service (brief downtime). The user always approves first."
    parameters = {"type": "object", "properties": _props(service=(S, "Service id")), "required": ["service"]}

    def describe(self, args):
        return f"restarts Render service {args.get('service')}"

    def _run(self, args):
        self.ctx.call("restart_service", args)
        return self.ok(f"Restart requested for {args['service']}.")


# -------------------------------------------------------------- Cloudflare
ZONE = (S, "Zone id (from cloudflare_zones)")


class CloudflareZones(ServiceTool):
    name = "cloudflare_zones"
    description = "List the user's Cloudflare zones (domains) with id, status and plan."
    parameters = {"type": "object", "properties": _props(name=(S, "Optional domain filter"), limit=LIMIT),
                  "required": []}

    def _run(self, args):
        zones = self.ctx.call("list_zones", args).get("result") or []
        lines = [f"{len(zones)} zone(s):"]
        lines += [f"- {z.get('name')} [{z.get('id')}] {z.get('status')}, account "
                  f"{(z.get('account') or {}).get('id', '?')}" for z in zones]
        return self.ok("\n".join(lines), {"zones": [z.get("id") for z in zones]})


class CloudflareDnsRecords(ServiceTool):
    name = "cloudflare_dns_records"
    description = "List DNS records of one Cloudflare zone (type, name, content, proxied, id)."
    parameters = {"type": "object", "properties": _props(zone=ZONE, type=(S, "Optional type filter, e.g. A"),
                                                         limit=LIMIT), "required": ["zone"]}

    def _run(self, args):
        records = self.ctx.call("list_dns_records", args).get("result") or []
        lines = [f"{len(records)} record(s):"]
        lines += [f"- {r.get('type')} {r.get('name')} -> {cut(r.get('content'), 120)} "
                  f"{'(proxied)' if r.get('proxied') else ''} [{r.get('id')}]" for r in records]
        return self.ok("\n".join(lines), {"records": [r.get("id") for r in records]})


class CloudflareWorkers(ServiceTool):
    name = "cloudflare_workers"
    description = "List the Workers scripts in a Cloudflare account (the account id is shown by cloudflare_zones)."
    parameters = {"type": "object", "properties": _props(account=(S, "Account id")), "required": ["account"]}

    def _run(self, args):
        scripts = self.ctx.call("list_workers", args).get("result") or []
        lines = [f"{len(scripts)} Worker(s):"]
        lines += [f"- {w.get('id')} (modified {w.get('modified_on', '?')})" for w in scripts]
        return self.ok("\n".join(lines), {"workers": [w.get("id") for w in scripts]})


_DNS_FIELDS = dict(type=(S, "Record type: A, AAAA, CNAME, TXT, MX..."), name=(S, "Record name, e.g. www"),
                   content=(S, "Value, e.g. an IP address"), ttl=(I, "TTL seconds (1 = automatic)"),
                   proxied=({"type": "boolean"}, "Route through Cloudflare"))


class CloudflareDnsCreate(ServiceTool):
    name = "cloudflare_dns_create"
    level = "always"
    description = "Create a DNS record in a Cloudflare zone. The user always approves first."
    parameters = {"type": "object", "properties": _props(zone=ZONE, **_DNS_FIELDS),
                  "required": ["zone", "type", "name", "content"]}

    def describe(self, args):
        return f"creates DNS record {args.get('type')} {args.get('name')} -> {cut(args.get('content'), 80)}"

    def _run(self, args):
        r = self.ctx.call("create_dns_record", args).get("result") or {}
        return self.ok(f"Created {r.get('type')} {r.get('name')} [{r.get('id')}].", {"id": r.get("id")})


class CloudflareDnsUpdate(ServiceTool):
    name = "cloudflare_dns_update"
    level = "always"
    description = "Change an existing Cloudflare DNS record. The user always approves first."
    parameters = {"type": "object", "properties": _props(zone=ZONE, record_id=(S, "Record id"), **_DNS_FIELDS),
                  "required": ["zone", "record_id"]}

    def describe(self, args):
        return f"changes DNS record {args.get('record_id')} (sites may stop resolving if this is wrong)"

    def _run(self, args):
        r = self.ctx.call("update_dns_record", args).get("result") or {}
        return self.ok(f"Updated {r.get('type')} {r.get('name')} -> {cut(r.get('content'), 120)}.", {"id": r.get("id")})


class CloudflareDnsDelete(ServiceTool):
    name = "cloudflare_dns_delete"
    level = "always"
    description = "Delete a Cloudflare DNS record. The user always approves first."
    parameters = {"type": "object", "properties": _props(zone=ZONE, record_id=(S, "Record id")),
                  "required": ["zone", "record_id"]}

    def describe(self, args):
        return f"deletes DNS record {args.get('record_id')} - this cannot be undone"

    def _run(self, args):
        self.ctx.call("delete_dns_record", args)
        return self.ok(f"Deleted DNS record {args['record_id']}.")


class CloudflarePurgeCache(ServiceTool):
    name = "cloudflare_purge_cache"
    level = "always"
    description = "Purge the whole Cloudflare cache of a zone. The user always approves first."
    parameters = {"type": "object", "properties": _props(zone=ZONE), "required": ["zone"]}

    def describe(self, args):
        return f"purges every cached file of zone {args.get('zone')} (origin load goes up)"

    def _run(self, args):
        self.ctx.call("purge_cache", args)
        return self.ok(f"Purged the cache of {args['zone']}.")


# ---------------------------------------------------------------- Supabase
PROJECT = (S, "Project ref (from supabase_projects)")


class SupabaseProjects(ServiceTool):
    name = "supabase_projects"
    description = "List the user's Supabase projects (name, ref, region, status)."
    parameters = {"type": "object", "properties": {}, "required": []}

    def _run(self, args):
        projects = self.ctx.call("list_projects", {}).get("items") or []
        lines = [f"{len(projects)} project(s):"]
        lines += [f"- {p.get('name')} [ref {p.get('id') or p.get('ref')}] {p.get('region')} {p.get('status')}"
                  for p in projects]
        return self.ok("\n".join(lines), {"projects": [p.get("id") or p.get("ref") for p in projects]})


class SupabaseTables(ServiceTool):
    name = "supabase_tables"
    description = "List the tables a Supabase project exposes through its REST API."
    parameters = {"type": "object", "properties": _props(project=PROJECT), "required": ["project"]}

    def _run(self, args):
        tables = self.ctx.call("list_tables", args).get("tables") or []
        return self.ok(f"{len(tables)} table(s): {', '.join(tables) or 'none visible with this key'}",
                       {"tables": tables})


class SupabaseSelect(ServiceTool):
    name = "supabase_select"
    description = ("Read rows from a Supabase table. Filters use PostgREST form column=op.value, e.g. "
                   "[\"status=eq.open\", \"age=gt.30\"].")
    parameters = {"type": "object", "properties": _props(
        project=PROJECT, table=(S, "Table name"), columns=(S, "Comma-separated columns (default *)"),
        filters=({"type": "array", "items": {"type": "string"}}, "Filters like status=eq.open"),
        limit=(I, "Rows (default 20, max 100)")), "required": ["project", "table"]}

    def _run(self, args):
        rows = self.ctx.call("select_rows", args).get("rows") or []
        text = json.dumps(rows, ensure_ascii=False, default=str)
        if len(text) > 6000:
            text = text[:6000] + " ... [cut]"
        return self.ok(f"{len(rows)} row(s) from {args['table']}:\n{self.untrusted()}\n{text}", {"count": len(rows)})


class SupabaseInsert(ServiceTool):
    name = "supabase_insert"
    level = "always"
    description = "Insert rows into a Supabase table. The user always approves first."
    parameters = {"type": "object", "properties": _props(
        project=PROJECT, table=(S, "Table name"),
        rows=({"type": "array", "items": {"type": "object"}}, "Rows to insert, e.g. [{\"title\": \"x\"}]")),
        "required": ["project", "table", "rows"]}

    def describe(self, args):
        count = len(args.get("rows") or []) if isinstance(args.get("rows"), list) else 1
        return f"inserts {count} row(s) into {args.get('table')} in project {args.get('project')}"

    def _run(self, args):
        rows = self.ctx.call("insert_rows", args).get("rows") or []
        return self.ok(f"Inserted {len(rows)} row(s) into {args['table']}.", {"count": len(rows)})


# ------------------------------------------------------------------- Canva
class CanvaDesigns(ServiceTool):
    name = "canva_designs"
    description = "List or search the user's Canva designs (title, id, last edit, edit link)."
    parameters = {"type": "object", "properties": _props(query=(S, "Optional search text"), limit=LIMIT),
                  "required": []}

    def _run(self, args):
        items = self.ctx.call("list_designs", args).get("items") or []
        lines = [f"{len(items)} design(s):"]
        lines += [f"- {cut(d.get('title') or '(untitled)', 100)} [{d.get('id')}] "
                  f"{(d.get('urls') or {}).get('edit_url', '')}" for d in items]
        return self.ok("\n".join(lines), {"designs": [d.get("id") for d in items]})


class CanvaExport(ServiceTool):
    name = "canva_export"
    description = "Export a Canva design to a file (pdf, png, jpg, gif, pptx, mp4) and return download links."
    parameters = {"type": "object", "properties": _props(
        design_id=(S, "Design id from canva_designs"),
        format=({"type": "string", "enum": ["pdf", "png", "jpg", "gif", "pptx", "mp4"]}, "File type (default pdf)")),
        "required": ["design_id"]}

    def _run(self, args):
        job = self.ctx.call("export_design", args).get("job") or {}
        status = job.get("status")
        if status == "success":
            urls = job.get("urls") or []
            return self.ok("Export ready (links expire after a few hours):\n" + "\n".join(urls), {"urls": urls})
        if status == "failed":
            return self.ok(f"Canva could not export it: {(job.get('error') or {}).get('message', 'unknown reason')}",
                           {"status": status})
        return self.ok(f"Export still running (job {job.get('id')}); ask again in a moment.", {"job": job.get("id")})


TOOLS: dict[str, tuple[type[ServiceTool], ...]] = {
    "github": (GitHubRepos, GitHubIssues, GitHubIssue, GitHubPulls, GitHubPull, GitHubFile,
               GitHubCreateIssue, GitHubComment),
    "slack": (SlackChannels, SlackHistory, SlackPost),
    "vercel": (VercelProjects, VercelDeployments, VercelLogs, VercelRedeploy),
    "render": (RenderServices, RenderDeploys, RenderLogs, RenderDeploy, RenderRestart),
    "cloudflare": (CloudflareZones, CloudflareDnsRecords, CloudflareWorkers, CloudflareDnsCreate,
                   CloudflareDnsUpdate, CloudflareDnsDelete, CloudflarePurgeCache),
    "supabase": (SupabaseProjects, SupabaseTables, SupabaseSelect, SupabaseInsert),
    "canva": (CanvaDesigns, CanvaExport),
}


def service_tools(service_id: str, context: ServiceContext) -> list[Tool]:
    return [cls(context) for cls in TOOLS.get(service_id, ())]
