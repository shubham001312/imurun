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
4. Commit, tag, push:
   ```
   git commit -am "release: vX.Y.Z"
   git tag vX.Y.Z
   git push origin main vX.Y.Z
   ```
5. Watch **Actions → Release**. Jobs, in order:
   `check` (tag = pyproject = package.json) → `test` (pytest on Ubuntu, Windows, macOS)
   and `engine` (MinGW / manylinux GCC / clang) → `wheels` (one per OS, each
   smoke-installed into a fresh venv on its own OS and started with
   `--no-open`, until `/health` answers) and `sdist` → `release` (GitHub Release +
   assets + `SHA256SUMS.txt`) → `pypi` → `npm`.
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
