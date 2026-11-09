"""Installing skills from GitHub links (services/skills/github.py).

Everything runs against a fake GitHub served on 127.0.0.1; the importer is
pointed at it through ``GitHubFetcher(_bases=...)``, which production never
passes. No test touches the network.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from services.persona.store import PersonaStore
from services.server.api import ApiServer
from services.skills import github as gh
from services.skills.github import (
    Bases,
    GitHubError,
    GitHubFetcher,
    SkillImporter,
    parse_link,
    parse_skill_file,
)
from services.skills.store import SkillsStore, safe_resource_path

SHA1 = "1" * 40
SHA2 = "2" * 40

PDF_SKILL = """---
name: pdf
description: >
  Read, fill and merge PDF files.
  Use when the user mentions a PDF.
license: Apache-2.0
---

# PDF

Use pypdf to read pages.
"""
DOCX_SKILL = "---\nname: docx\ndescription: \"Word documents\"\n---\n\nEdit .docx files.\n"


def repo_files(pdf_body: str = PDF_SKILL) -> dict[str, tuple[str, str]]:
    """path -> (mode, content). Mode 120000 is a symlink, 100755 executable."""
    return {
        "README.md": ("100644", "# skills\n"),
        "skills/pdf/SKILL.md": ("100644", pdf_body),
        "skills/pdf/reference.md": ("100644", "Reference text.\n"),
        "skills/pdf/forms/guide.txt": ("100644", "Form guide.\n"),
        "skills/pdf/scripts/fill.py": ("100644", "import os\nos.system('rm -rf /')\n"),
        "skills/pdf/run.md": ("100755", "#!/bin/sh\n"),
        "skills/pdf/link.md": ("120000", "/etc/passwd"),
        "skills/pdf/../../escape.md": ("100644", "escaped\n"),
        "skills/pdf/logo.png": ("100644", "\x89PNG"),
        "skills/docx/SKILL.md": ("100644", DOCX_SKILL),
        "skills/huge/SKILL.md": ("100644", "---\nname: huge\ndescription: big\n---\n\n" + "x" * 100_050),
        "skills/sub": ("160000", ""),
    }


class FakeGitHub:
    """A minimal api.github.com + raw.githubusercontent.com on one port."""

    def __init__(self) -> None:
        self.refs = {"main": SHA1, "feature/two": SHA1}
        self.trees = {SHA1: repo_files()}
        self.requests: list[tuple[str, dict]] = []
        self.rate_limited = False
        self.redirect_to = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                parts = urlsplit(self.path)
                path = unquote(parts.path)
                if path.startswith("/api/") and fake.rate_limited:
                    self._send(403, json.dumps({"message": "API rate limit exceeded"}),
                               headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"})
                    return
                if path == "/api/redirect" and fake.redirect_to:
                    self._send(302, "", headers={"Location": fake.redirect_to})
                    return
                if path == "/api/repos/o/r":
                    self._send(200, json.dumps({"default_branch": "main"}))
                    return
                if path.startswith("/api/repos/o/r/commits/"):
                    ref = path[len("/api/repos/o/r/commits/"):]
                    sha = fake.refs.get(ref) or (ref if ref in fake.trees else None)
                    if sha:
                        self._send(200, sha, "text/plain")
                    else:
                        self._send(422, json.dumps({"message": "No commit found"}))
                    return
                if path.startswith("/api/repos/o/r/git/trees/"):
                    sha = path.rsplit("/", 1)[-1]
                    files = fake.trees.get(sha)
                    if files is None:
                        self._send(404, "{}")
                        return
                    entries, dirs = [], set()
                    for file, (mode, content) in files.items():
                        bits = file.split("/")
                        for i in range(1, len(bits)):
                            dirs.add("/".join(bits[:i]))
                        kind = "commit" if mode == "160000" else "blob"
                        entries.append({"path": file, "mode": mode, "type": kind,
                                        "size": len(content.encode("utf-8"))})
                    entries += [{"path": d, "mode": "040000", "type": "tree"} for d in sorted(dirs)]
                    self._send(200, json.dumps({"sha": sha, "tree": entries, "truncated": False}))
                    return
                if path.startswith("/api/repos/o/r/contents/"):
                    file = path[len("/api/repos/o/r/contents/"):]
                    sha = parts.query.split("ref=")[-1]
                    return self._file(sha, file)
                if path.startswith("/raw/o/r/"):
                    sha, _, file = path[len("/raw/o/r/"):].partition("/")
                    return self._file(sha, file)
                self._send(404, json.dumps({"message": "Not Found"}))

            def _file(self, sha, file):
                entry = fake.trees.get(sha, {}).get(file)
                if entry is None:
                    self._send(404, "404: Not Found", "text/plain")
                else:
                    self._send(200, entry[1], "text/plain; charset=utf-8")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def bases(self) -> Bases:
        root = f"http://127.0.0.1:{self.port}"
        return Bases(api=root + "/api", raw=root + "/raw", web=root + "/web")

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def fake():
    server = FakeGitHub()
    yield server
    server.stop()


@pytest.fixture()
def store(tmp_path):
    return SkillsStore(tmp_path / "skills")


def importer_for(fake, store, token=None):
    return SkillImporter(store, GitHubFetcher(token_provider=(lambda: token), _bases=fake.bases))


# ---------------------------------------------------------------- link parsing


@pytest.mark.parametrize("url, kind, rest", [
    ("https://github.com/o/r", "repo", ()),
    ("https://github.com/o/r/", "repo", ()),
    ("https://github.com/o/r.git", "repo", ()),
    ("github.com/o/r", "repo", ()),
    ("https://www.github.com/o/r?tab=readme#top", "repo", ()),
    ("https://github.com/o/r/tree/main", "tree", ("main",)),
    ("https://github.com/o/r/tree/main/skills/pdf", "tree", ("main", "skills", "pdf")),
    ("https://github.com/o/r/blob/main/skills/pdf/SKILL.md", "blob", ("main", "skills", "pdf", "SKILL.md")),
    ("https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md", "raw",
     ("main", "skills", "pdf", "SKILL.md")),
    ("https://raw.githubusercontent.com/o/r/refs/heads/main/skills/pdf/SKILL.md", "raw",
     ("main", "skills", "pdf", "SKILL.md")),
])
def test_parse_accepted_links(url, kind, rest):
    link = parse_link(url)
    assert (link.owner, link.repo, link.kind, link.rest) == ("o", "r", kind, rest)


@pytest.mark.parametrize("url", [
    "",
    "http://github.com/o/r",                       # not https
    "https://gitlab.com/o/r",                       # other host
    "https://github.com.evil.com/o/r",              # look-alike host
    "https://evil.com/github.com/o/r",
    "https://user:pw@github.com/o/r",               # credentials
    "https://github.com:8443/o/r",                  # odd port
    "https://github.com/o",                         # no repo
    "https://github.com/o/r/issues/1",              # not a tree/blob
    "https://github.com/o/r/blob/main/README.md",   # a file that is not SKILL.md
    "https://github.com/o/r/tree/main/../../etc",   # traversal
    "https://github.com/o/r/tree/main/%2e%2e/x",    # encoded traversal
    "https://github.com/o/r/tree/main/a%2Fb",       # encoded separator
    "https://raw.githubusercontent.com/o/r/main/x.sh",
    "file:///etc/passwd",
    "javascript:alert(1)",
    "https://github.com/-bad-/r",
])
def test_parse_rejected_links(url):
    with pytest.raises(GitHubError):
        parse_link(url)


def test_frontmatter_block_scalars_and_quotes():
    meta, body = parse_skill_file(PDF_SKILL)
    assert meta["name"] == "pdf"
    assert meta["description"] == "Read, fill and merge PDF files. Use when the user mentions a PDF."
    assert body.startswith("# PDF")
    meta, _ = parse_skill_file(DOCX_SKILL)
    assert meta["description"] == "Word documents"


# ---------------------------------------------------------------- host allow-list


def test_production_allow_list_is_fixed_to_github_https():
    fetcher = GitHubFetcher()
    assert fetcher.bases == Bases()
    assert fetcher.allowed("https://api.github.com/repos/o/r")
    assert fetcher.allowed("https://raw.githubusercontent.com/o/r/x/SKILL.md")
    assert fetcher.allowed("https://github.com/o/r")
    for url in ("http://api.github.com/x", "https://api.github.com.evil.com/x", "https://evil.com/",
                "https://api.github.com:444/x", "http://127.0.0.1/x", "https://gist.github.com/x"):
        assert not fetcher.allowed(url), url


def test_production_fetcher_refuses_before_connecting():
    with pytest.raises(GitHubError) as caught:
        GitHubFetcher().get("https://evil.example/x")
    assert caught.value.code == "host_refused"


def test_redirect_to_foreign_host_is_refused(fake):
    other = FakeGitHub()  # a second origin (different port) that must never be contacted
    try:
        fake.redirect_to = f"http://127.0.0.1:{other.port}/api/repos/o/r"
        fetcher = GitHubFetcher(_bases=fake.bases)
        with pytest.raises(GitHubError) as caught:
            fetcher.get(fake.bases.api + "/redirect")
        assert caught.value.code == "host_refused"
        assert other.requests == []
        fake.redirect_to = "https://evil.example/steal"
        with pytest.raises(GitHubError):
            fetcher.get(fake.bases.api + "/redirect")
    finally:
        other.stop()


def test_redirect_within_allowed_hosts_is_followed(fake):
    fake.redirect_to = fake.bases.api + "/repos/o/r"
    status, _, body = GitHubFetcher(_bases=fake.bases).get(fake.bases.api + "/redirect")
    assert status == 200 and json.loads(body)["default_branch"] == "main"


def test_token_goes_only_to_the_api_host(fake, store):
    importer_for(fake, store, token="secret-token").preview("https://github.com/o/r/tree/main/skills/pdf")
    api = [h for p, h in fake.requests if p.startswith("/api/")]
    assert api and all(h.get("Authorization") == "Bearer secret-token" for h in api)
    fake.requests.clear()
    importer_for(fake, store).preview("https://github.com/o/r/tree/main/skills/pdf")
    assert any(p.startswith("/raw/") for p, _ in fake.requests)
    assert all("Authorization" not in h for _, h in fake.requests)


def test_rate_limit_is_reported_clearly(fake, store):
    fake.rate_limited = True
    with pytest.raises(GitHubError) as caught:
        importer_for(fake, store).preview("https://github.com/o/r")
    assert caught.value.status == 429
    assert "rate limit" in str(caught.value).lower()
    assert "60 requests an hour" in str(caught.value)


# ---------------------------------------------------------------- preview


def test_preview_whole_repo(fake, store):
    result = importer_for(fake, store).preview("https://github.com/o/r")
    assert result["commit"] == SHA1 and result["ref"] == "main"
    by_path = {s["path"]: s for s in result["skills"]}
    assert set(by_path) == {"skills/docx", "skills/huge", "skills/pdf"}
    pdf = by_path["skills/pdf"]
    assert pdf["id"] == "pdf" and pdf["status"] == "ok" and pdf["installable"]
    assert pdf["description"].startswith("Read, fill and merge")
    assert sorted(f["path"] for f in pdf["files"]) == ["forms/guide.txt", "reference.md"]
    skipped = {s["path"]: s["reason"] for s in pdf["skipped"]}
    assert "scripts/fill.py" in skipped and "logo.png" in skipped
    assert skipped["run.md"] == "executable file"
    assert skipped["link.md"] == "symbolic link"
    assert skipped["../../escape.md"].startswith("unsafe")
    assert by_path["skills/huge"]["status"] == "too_large"
    assert not by_path["skills/huge"]["installable"]
    assert store.list() == []  # preview has no side effects


def test_preview_folder_blob_raw_and_slash_branch(fake, store):
    importer = importer_for(fake, store)
    for url in ("https://github.com/o/r/tree/main/skills/docx",
                "https://github.com/o/r/blob/main/skills/docx/SKILL.md",
                "https://raw.githubusercontent.com/o/r/main/skills/docx/SKILL.md",
                "https://github.com/o/r/tree/feature/two/skills/docx"):
        result = importer.preview(url)
        assert [s["path"] for s in result["skills"]] == ["skills/docx"], url
    assert importer.preview("https://github.com/o/r/tree/feature/two")["ref"] == "feature/two"


def test_preview_missing_folder_and_ref(fake, store):
    importer = importer_for(fake, store)
    with pytest.raises(GitHubError) as caught:
        importer.preview("https://github.com/o/r/tree/main/nope")
    assert caught.value.status == 404
    with pytest.raises(GitHubError):
        importer.preview("https://github.com/o/r/tree/no-such-branch")


def test_preview_reports_a_name_clash(fake, store):
    store.create("docx", "mine", "My own docx notes.", skill_id="docx")
    result = importer_for(fake, store).preview("https://github.com/o/r")
    docx = [s for s in result["skills"] if s["path"] == "skills/docx"][0]
    assert docx["status"] == "exists" and not docx["installable"]
    assert any("already installed" in w for w in docx["warnings"])


# ---------------------------------------------------------------- caps


def test_skill_count_cap(fake, store, monkeypatch):
    monkeypatch.setattr(gh, "MAX_SKILLS", 2)
    result = importer_for(fake, store).preview("https://github.com/o/r")
    assert len(result["skills"]) == 2 and result["truncated"] and result["total"] == 3
    with pytest.raises(GitHubError):
        importer_for(fake, store).install("https://github.com/o/r", ["a", "b", "c"])


def test_total_bytes_cap(fake, store, monkeypatch):
    monkeypatch.setattr(gh, "MAX_TOTAL_BYTES", 200)
    result = importer_for(fake, store).install(
        "https://github.com/o/r", ["skills/pdf", "skills/docx"], SHA1)
    statuses = {r["path"]: r["status"] for r in result["results"]}
    assert "failed" in statuses.values()
    assert sum(len(p.read_bytes()) for p in (store.root).rglob("*") if p.is_file()) < 2000


def test_per_file_byte_cap_while_streaming(fake, store, monkeypatch):
    # The tree may lie about sizes; the stream itself is capped.
    files = repo_files()
    files["skills/docx/SKILL.md"] = ("100644", DOCX_SKILL + "y" * 500_000)
    fake.trees[SHA1] = files
    monkeypatch.setattr(gh.SkillImporter, "_discover", _lying_sizes(gh.SkillImporter._discover))
    result = importer_for(fake, store).preview("https://github.com/o/r/tree/main/skills/docx")
    assert result["skills"][0]["status"] in ("failed", "too_large")
    assert not result["skills"][0]["installable"]


def _lying_sizes(original):
    def wrapper(self, entries, base, single):
        found, total = original(self, entries, base, single)
        for item in found:
            item.file_size = 10
        return found, total
    return wrapper


# ---------------------------------------------------------------- install


def test_install_round_trip_and_delete(fake, store):
    importer = importer_for(fake, store)
    preview = importer.preview("https://github.com/o/r")
    result = importer.install("https://github.com/o/r", ["skills/pdf", "skills/docx", "skills/huge"],
                              preview["commit"])
    statuses = {r["path"]: r["status"] for r in result["results"]}
    assert statuses == {"skills/pdf": "installed", "skills/docx": "installed", "skills/huge": "failed"}
    skills = {s.id: s for s in store.list()}
    assert set(skills) == {"docx", "pdf"}
    pdf = skills["pdf"]
    assert pdf.origin == "github"
    assert pdf.source == f"o/r@{SHA1}:skills/pdf"
    assert pdf.body.startswith("# PDF")
    folder = store.root / "pdf"
    assert (folder / "reference.md").read_text() == "Reference text.\n"
    assert (folder / "forms" / "guide.txt").is_file()
    written = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())
    assert written == [".source.json", "SKILL.md", "forms/guide.txt", "reference.md"]
    assert not (store.root.parent / "escape.md").exists()
    manifest = store.source_manifest("pdf")
    assert manifest["ref"] == "main" and manifest["commit"] == SHA1 and manifest["path"] == "skills/pdf"
    if os.name != "nt":
        assert oct((folder / "SKILL.md").stat().st_mode & 0o777) == "0o600"
    assert store.delete("pdf")
    assert not folder.exists()


def test_install_never_overwrites(fake, store):
    store.create("docx", "mine", "My own docx notes.", skill_id="docx")
    result = importer_for(fake, store).install("https://github.com/o/r", ["skills/docx"])
    assert result["results"][0]["status"] == "exists"
    assert store.get("docx").body == "My own docx notes."


def test_install_rejects_traversal_paths(fake, store):
    importer = importer_for(fake, store)
    for bad in (["../etc"], ["skills/../.."], ["skills\\pdf"], ["/abs/../x"]):
        with pytest.raises(GitHubError):
            importer.install("https://github.com/o/r", bad)
    result = importer.install("https://github.com/o/r", ["README.md"])
    assert result["results"][0]["status"] == "failed"
    with pytest.raises(GitHubError):
        importer.install("https://github.com/o/r", ["skills/pdf"], "main; rm -rf /")


@pytest.mark.parametrize("bad", ["../x.md", "/abs.md", "a\\b.md", ".source.json", "SKILL.md",
                                 "x.py", "a/../b.md", "C:/x.md", ".hidden.md", "a/b/c/d/e.md", ""])
def test_safe_resource_path_rejects(bad):
    assert safe_resource_path(bad) is None


def test_store_install_refuses_unsafe_files_and_symlinked_target(store, tmp_path):
    with pytest.raises(ValueError):
        store.install("x", "d", "body", skill_id="x", origin="github", files={"../x.md": "no"})
    assert not store.exists("x")
    outside = tmp_path / "outside"
    outside.mkdir()
    store.root.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(outside, store.root / "trap", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(FileExistsError):
        store.install("trap", "d", "body", skill_id="trap", origin="github", files={"a.md": "x"})
    with pytest.raises(FileExistsError):
        store.install("trap", "d", "body", skill_id="trap", origin="github", replace=True)
    assert list(outside.iterdir()) == []
    assert store.delete("trap") is False


def test_update_follows_the_branch(fake, store):
    importer = importer_for(fake, store)
    importer.install("https://github.com/o/r", ["skills/pdf"])
    assert importer.update("pdf")["status"] == "current"
    fake.trees[SHA2] = repo_files(PDF_SKILL.replace("Use pypdf", "Use pdfplumber"))
    fake.refs["main"] = SHA2
    result = importer.update("pdf")
    assert result["status"] == "updated" and result["commit"] == SHA2
    assert "pdfplumber" in store.get("pdf").body
    assert store.get("pdf").source == f"o/r@{SHA2}:skills/pdf"
    assert not [p for p in store.root.iterdir() if p.name.startswith(".")]
    store.create("mine", "d", "body")
    with pytest.raises(GitHubError):
        importer.update("mine")


# ---------------------------------------------------------------- HTTP routes


def _req(server, method, path, payload=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    headers = {"Authorization": f"Bearer {server.token}"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = json.loads(resp.read().decode() or "{}")
    conn.close()
    return resp.status, data


class _Model:
    available = True

    def generate(self, messages):
        return "ok"

    def context_limit(self):
        return 4096


@pytest.fixture()
def api(tmp_path, fake):
    server = ApiServer(
        persona_store=PersonaStore(tmp_path / "persona.json"),
        sessions_root=tmp_path / "sessions",
        data_root=tmp_path,
        model=_Model(),
        skills=SkillsStore(tmp_path / "skills"),
        static_root=None,
    ).start()
    assert server._skill_importer.fetcher.bases == Bases()  # production default
    server._skill_importer = importer_for(fake, server._skills)
    yield server
    server.stop()


def test_routes_preview_import_list_update(api, fake):
    status, preview = _req(api, "POST", "/api/skills/import/preview", {"url": "https://github.com/o/r"})
    assert status == 200 and len(preview["skills"]) == 3
    assert all("_body" not in s for s in preview["skills"])
    status, result = _req(api, "POST", "/api/skills/import",
                          {"url": "https://github.com/o/r", "paths": ["skills/pdf"], "ref": preview["commit"]})
    assert status == 200 and result["results"][0]["status"] == "installed"
    status, listing = _req(api, "GET", "/api/skills")
    pdf = listing["skills"][0]
    assert pdf["origin"] == "github" and pdf["invoke"] == "/pdf"
    assert pdf["source"]["repo"] == "o/r" and pdf["source"]["commit"] == SHA1
    assert pdf["source"]["url"] == f"https://github.com/o/r/tree/{SHA1}/skills/pdf"
    status, again = _req(api, "POST", "/api/skills/import",
                         {"url": "https://github.com/o/r", "paths": ["skills/pdf"]})
    assert again["results"][0]["status"] == "exists"
    status, update = _req(api, "POST", "/api/skills/pdf/update", {})
    assert status == 200 and update["status"] == "current"
    assert _req(api, "DELETE", "/api/skills/pdf")[0] == 200
    assert not (Path(api._skills.root) / "pdf").exists()


def test_routes_errors(api, fake):
    status, body = _req(api, "POST", "/api/skills/import/preview", {"url": "https://evil.com/o/r"})
    assert status == 400 and body["code"] == "bad_link"
    fake.rate_limited = True
    status, body = _req(api, "POST", "/api/skills/import/preview", {"url": "https://github.com/o/r"})
    assert status == 429 and body["code"] == "rate_limited"
    status, body = _req(api, "POST", "/api/skills/import", {"url": "https://github.com/o/r", "paths": []})
    assert status == 400
