# Releasing IMURUN (PyPI + npm + GitHub Release)

One tag (`v<version>`) publishes everything:

| Channel | What users run | What gets published |
|---|---|---|
| PyPI `imurun` | `pip install imurun` → `imurun` | 3 platform wheels with the engine, 1 pure wheel, the sdist |
| npm `imurun` | `npx imurun` / `npm i -g imurun` | the launcher (`package.json` + `npm/imurun.js`), which installs `imurun==<same version>` from PyPI |
| GitHub Release | `imurun --get-engine`, manual downloads | `fish-windows-x64.dll`, `libfish-linux-x64.so`, `libfish-macos-arm64.dylib`, `SHA256SUMS.txt`, all distributions |

Workflows: `.github/workflows/workflow.yml` (release), `ci.yml` (every push and PR),
`engine.yml` (engine build, called by both). Packaging details are in
`docs/setup/README.md` ("Install as a package").

## History you should know

- PyPI and npm already have a project called `imurun`: versions **0.1.0–0.1.11**
  (Oct 2026), published from this GitHub repo (`shubham001312/imurun`). That was
  the earlier Bun/TypeScript CLI. The Python package was a launcher that downloaded
  that CLI's executables. 0.3.0 is the first release of the current Python app
  under the same names. It uses the same command name (`imurun`) and the same
  publishing setup: the release workflow is still called `workflow.yml`, and PyPI
  still publishes through environment `pypi`.
- The old 0.1.x PyPI trusted publisher was registered for owner `shubham001312`,
  repository `imurun`, workflow `workflow.yml`, environment `pypi`. If it is still in
  place on PyPI, no PyPI setup is needed (check step 1 below).
- Since versions only go up, the next release must be > 0.1.11. It is 0.3.0.

## One-time setup

### 1. PyPI trusted publishing (no token anywhere)

On https://pypi.org/manage/project/imurun/settings/publishing/ (signed in as an owner
of `imurun`), check that a GitHub publisher exists with:

| Field | Value |
|---|---|
| Owner | `shubham001312` |
| Repository | `imurun` |
| Workflow name | `workflow.yml` |
| Environment | `pypi` |

If it is missing, add it there ("Add a new publisher" → GitHub). A *pending*
publisher (https://pypi.org/manage/account/publishing/) is only for a project name
that does not exist yet. `imurun` exists, so use the project's own page. If you ever
publish under a new name, create a pending publisher with the same four values.

On GitHub: **Settings → Environments → New environment → `pypi`**. Optionally add
yourself as a required reviewer, so every PyPI upload waits for one click.

### 2. npm

Pick one:
- **Token (simplest).** On npmjs.com: Access Tokens → Generate New Token → Granular,
  with *Read and write* on package `imurun` and "bypass 2FA" allowed for automation.
  Then on GitHub: **Settings → Secrets and variables → Actions → New repository
  secret** `NPM_TOKEN`.
- **Trusted publishing (no secret).** On npmjs.com: package `imurun` → Settings →
  Trusted publisher → GitHub Actions, with repository `shubham001312/imurun`,
  workflow `workflow.yml` and environment `npm`. The job installs npm ≥ 11.5, which
  exchanges the OIDC credential on its own.

Also create the GitHub environment **`npm`** (Settings → Environments).

### 3. Repository settings

- **Settings → Actions → General → Workflow permissions:** "Read repository contents".
  The release job asks for `contents: write` itself, to create the GitHub Release.
- Actions must be allowed to run third-party actions: `msys2/setup-msys2`,
  `pypa/gh-action-pypi-publish`, plus the `actions/*` ones.

### 4. Code signing and provenance (publisher identity)

Without a signature, Windows Smart App Control / SmartScreen can block or flag the engine
(`fish.dll`) as coming from an "unknown publisher", and macOS Gatekeeper can call a downloaded
`libfish.dylib` the work of an "unidentified developer". The release pipeline handles each part
automatically once the credentials exist. Each part is skipped (with a warning) until then, so
releases never block on it.

| What | Status | What you need to do |
|---|---|---|
| **Build provenance** (Sigstore, SLSA) for every release file: wheels, sdist, engine libraries, `SHA256SUMS.txt` | **automatic, free** (`actions/attest-build-provenance`) | nothing. Verify with `gh attestation verify <file> -R shubham001312/imurun` |
| **PyPI attestations** (PEP 740): PyPI shows each file as built by `workflow.yml` | **automatic** with trusted publishing | nothing |
| **npm provenance** | automatic (`npm publish --provenance`) | nothing |
| **Windows Authenticode** on `fish.dll` | runs when configured | an [Azure Artifact Signing](https://learn.microsoft.com/azure/artifact-signing/) account (identity-validated; paid monthly), an app registration with a federated credential for this repo (OIDC). Repository **variables** `AZURE_SIGNING_ENDPOINT`, `AZURE_SIGNING_ACCOUNT`, `AZURE_CERT_PROFILE`; **secrets** `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` |
| **macOS Developer ID + notarization** on `libfish.dylib` | runs when configured; otherwise ad-hoc signed (loads, but no identity) | an Apple Developer Program membership. Export the "Developer ID Application" certificate as .p12. **Variables** `APPLE_SIGNING_IDENTITY` (e.g. `Developer ID Application: ImuTechz (TEAMID)`), `APPLE_TEAM_ID`; **secrets** `APPLE_CERT_P12` (base64 of the .p12), `APPLE_CERT_PASSWORD`, `APPLE_ID`, `APPLE_APP_PASSWORD` (an app-specific password) |

Buying the Windows and Apple identities is the only part that cannot be automated, because
both vendors validate who you are. The release and engine workflows do the rest:
`engine.yml` signs and verifies, and `workflow.yml` attests.

## Git: state of this folder and first push

This folder **is already a git repository** (remote
`origin = https://github.com/shubham001312/imurun.git`, branch `main`). On
2026-11-02 it had a large uncommitted working tree, and local `main` and
`origin/main` had diverged (37 local commits vs 2 remote: a merged PR from another
contributor, "fix(grep): match bare include globs", plus "The upgrade works however
IMURUN was installed"). Nothing has been committed or pushed for you. Suggested
order:

```
cd imurun
git status                          # review: many new docs/, frontend, services files
git rm --cached bin/fish.dll        # the engine is now a build artifact (CI builds it)
git add -A
git status --ignored                # check nothing secret is staged and no source is ignored
git commit -m "release: pip/npm packaging, CI, release workflow (0.3.0)"

# Reconcile with GitHub. origin/main has 2 commits you do not have:
git fetch origin
git log --oneline main..origin/main # see them
git merge origin/main               # or: git rebase origin/main; resolve conflicts
git push origin main
```

Never commit `auth_debug.txt` / `http_debug.txt`. They are debug dumps that may hold
request headers or tokens, and `.gitignore` (`*_debug.txt`) keeps them out. Consider
deleting them. They are also excluded from the sdist.

If you start from a fresh copy without `.git` instead:

```
git init -b main
git add -A && git status --ignored
git commit -m "IMURUN 0.3.0"
git remote add origin https://github.com/shubham001312/imurun.git
git fetch origin && git merge origin/main --allow-unrelated-histories   # keep the remote history
git push -u origin main
```

## Cutting a release

1. **Bump the version in both files** (CI refuses a mismatch):
   - `pyproject.toml` → `[project] version = "X.Y.Z"` (`imurun --version` and the wheel
     read this)
   - `package.json` → `"version": "X.Y.Z"` (the npm launcher installs `imurun==X.Y.Z`)
2. Add an entry to `docs/setup/CHANGELOG.md` (and to the other areas' changelogs).
3. Check locally (optional, this is what CI does):
   ```
   python -m pip install -r requirements-dev.txt
   python -m pytest -q
   python -m build                          # sdist + pure wheel in dist/
   python -m build --wheel                  # + this OS's wheel when bin/fish.dll exists
   python .github/scripts/smoke_install.py "dist/imurun-*-py3-none-win_amd64.whl" --expect-engine
   npm pack --dry-run
   ```
4. Commit and merge (or push) to `main`. **That is the release.** On every push to `main`,
   the Release workflow's `check` job asks whether `vX.Y.Z` is already tagged. If it is,
   nothing else runs. If it is not, the full pipeline runs, and the `release` job creates
   the tag on that commit together with the GitHub Release. The tag is created with
   `GITHUB_TOKEN`, so it does not start a second run. A failed release leaves no tag, and
   the next push to `main` tries again. Releases never overlap (`concurrency: release`).
   Pushing a tag yourself still works (`git tag vX.Y.Z && git push origin vX.Y.Z`), and so
   does **Actions → Release → Run workflow**: a dry run by default, or a real release with
   `dry_run` unticked.
   > Merging a version bump publishes to PyPI and npm, and a PyPI version can never be
   > uploaded again. Bump the version only in the commit that is meant to ship.
5. Watch **Actions → Release**. Jobs, in order:
   `check` (tag = pyproject = package.json; release or not) → `test` (pytest on Ubuntu, Windows, macOS)
   and `engine` (MinGW / manylinux GCC / clang) → `wheels` (one per OS, each
   smoke-installed into a fresh venv on its own OS and started with
   `--no-open`, until `/health` answers) and `sdist` → `release` (GitHub Release +
   assets + `SHA256SUMS.txt`) → `pypi` → `npm` (then `npm deprecate imurun@<0.2.0`,
   best effort, needs `NPM_TOKEN`).
6. Verify from a clean machine or venv:
   ```
   pip install --upgrade imurun && imurun --version
   npx imurun@X.Y.Z --version
   ```

**Dry run without publishing:** Actions → Release → *Run workflow* (`dry_run` is on
by default). This builds, tests and smoke-installs everything. The `release`, `pypi`
and `npm` jobs run only for a tag push.

**If a release fails halfway:** fix the cause and re-run the failed jobs. The jobs
can be repeated safely: the GitHub Release uploads with `--clobber`, PyPI uses
`skip-existing`, and npm skips a version that is already published. PyPI never
accepts the same file name twice, so after changing code, bump the version.

## What the engine matrix does and does not cover

| Platform | Built | Compiler | Vulkan | Tested in CI |
|---|---|---|---|---|
| Windows x64 | yes | MinGW-w64 GCC (MSYS2 UCRT64), `-static` | headers from MSYS2, loader at run time | DLL imports checked (no libstdc++/libgcc/winpthread), wheel loads it |
| Linux x64 | yes | GCC in `manylinux_2_28` | pinned Vulkan-Headers clone | wheel loads it |
| macOS arm64 | yes | Apple clang | **off** (`-DFISH_VULKAN=OFF`, MoltenVK is untested) | wheel loads it |
| Linux arm64, macOS x64, Windows arm64 | no | — | — | users get the pure wheel and build from source |

MSVC is not supported (GCC target attributes in the kernels). The C++ tests
`fish_test_m1/m2` are compiled but not run in CI, because they need a GGUF model.
Run them locally before releasing engine changes (CLAUDE.md, "Build and test").
