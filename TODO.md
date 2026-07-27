# TODO, Modernization things

Review findings as of 2026-07-26 against v1.2.0, revised 2026-07-27 when the project
direction changed to **headless only**.

## Direction

Qt is being removed entirely. The maintainer only needs the headless interface, and the
upstream project this forks from sees little use, so there is no compatibility burden to
carry. The HTTP server and web dashboard become the only interface.

Measured before deciding:

| Layer | Lines | Fate |
|-------|-------|------|
| `MainWindow` class in `NeewerLux.py` | 3,092 | delete |
| `neewerlux_ui.py` | 1,020 | delete |
| `neewerlux_anim_editor.py` | 844 | delete |
| `neewerlux_theme.py`, Qt stylesheet half | ~150 | delete, keep the web CSS |
| **Qt total** | **~5,100** | |
| BLE protocol and per-model light specs | 716 | keep, this is the irreplaceable part |
| Prefs and preset I/O | 967 | keep |
| HTTP server and API | 432 | keep, becomes the only interface |
| Animation engine | 431 | keep |
| Animation templates | 174 | keep |
| CLI parsing, BLE worker | 388 | keep |
| `neewerlux_webui.py` | 921 | keep, becomes the only UI |

Coupling into the Qt class from the rest of the code is 72 references, and nearly all are
UI notifications already guarded by `if mainWindow is not None`. Only `_savePresetsQuick`
needs extracting to a module-level function; `selectedLights` already has a headless
fallback (`if hasGUI else [-1]`), and the rest die with the window.

**Accepted losses:** global hotkeys (303 sites) and the system tray cannot exist without a
native shell. The visual animation and preset editors go too; animations and presets are
already plain files that can be edited directly, and the HTTP API covers playback.

## Revised order

Formatting and the module split both moved after the Qt removal, so neither churns code
that is about to be deleted.

| # | Item | PR | State |
|---|------|----|-------|
| 1 | Packaging | [#1](https://github.com/lugoues/NeewerLux/pull/1) | **merged** |
| 2 | Repository hygiene | [#2](https://github.com/lugoues/NeewerLux/pull/2) | **merged** |
| 3 | HTTP server security | [#3](https://github.com/lugoues/NeewerLux/pull/3) | open, findings addressed |
| 11 | JSON content negotiation | [#4](https://github.com/lugoues/NeewerLux/pull/4) | open, narrowed to negotiation |
| 4 | Drop PySide2 fallback | [#5](https://github.com/lugoues/NeewerLux/pull/5) | open, **superseded by item 13** |
| 13 | **Remove Qt entirely** | | next |
| 10 | Formatting (`ruff format`) | | after 13, half as much code to format |
| 5 | Tests and lint in CI | | after 13, no Qt in CI to worry about |
| 6 | Light dataclass | | after tests |
| 7 | Module split | | after 6, much simpler with no Qt |
| 8 | Config to TOML | | after 7 |
| 9 | Docker, signing, attestations | | after 8, now the primary artifact |
| 12 | Renovate | | any time after 5 |

Checkboxes below are ticked when the work is written, not when it is merged.

**Scope moved out of #4 and into item 7 (module split):** reporting real dispatch
outcomes over HTTP. `processCommands` and `processHTMLCommands` parse and forget, so the
HTTP layer cannot tell an accepted command from a dropped one. Three attempts to report it
truthfully each produced a new defect. It needs the request path restructured so the
dispatch can be awaited.

---

## 1. Packaging

There is no `pyproject.toml`, no `requirements.txt`, and no lockfile.

- [x] Add `pyproject.toml` with the real dependency set (`PySide6`, `bleak`, plus
      `pyinstaller` as a build extra) and `requires-python = ">=3.10"`.
- [x] Generate `uv.lock`. `uv` is already declared in `.mise/config.toml` but nothing
      uses it.
- [x] Fix `README.md` line 29. It instructs `pip install -r requirements.txt` and that
      file does not exist, so the documented install path fails.
- [x] Change `.github/workflows/release.yml` line 30 from
      `pip install PySide6 bleak pyinstaller` to `uv sync --frozen`. Unpinned installs
      mean every release binary is built against whatever resolved that day, and old
      builds cannot be reproduced.
- [x] Reconcile the Python version. Three places currently disagree: `README.md` says
      3.8+, CI pins 3.12, `.mise/config.toml` says `latest`. Current PySide6 (6.11.1)
      and bleak (3.0.2) both declare `requires_python >= 3.10`, so 3.8 is not just EOL,
      it is impossible with current dependencies. Pin mise and CI to the same version.

## 2. Repository hygiene

No `.gitignore` exists. The following are tracked and ship to every clone.

- [x] Add `.gitignore`.
- [x] `git rm --cached -r .vs/`. Includes `slnx.sqlite`, `.wsuo`, and the Copilot index
      databases `CodeChunks.db` and `SemanticSymbols.db`.
- [x] `git rm --cached light_prefs/NeewerLux.geometry light_prefs/customLights.prefs`.
      These are personal runtime state, including window position and light MAC
      addresses.
- [x] Ship default prefs under a distinct filename so user state never collides with a
      tracked file.
- [x] Keep the 101 animation JSONs tracked. Those are content, not state.

## 3. HTTP server security

The API turns GET requests into physical device commands, which is what makes these
worth fixing.

- [x] Remove or gate `Access-Control-Allow-Origin: *` at `NeewerLux.py:6202`. With no
      auth token, any website the user visits can issue commands, because the request
      originates from the user's own machine whose IP is on the allowlist. The IP
      allowlist provides no protection against this. **Highest priority of the three,
      the only one reachable from outside the LAN.**
- [x] Default the bind address to `127.0.0.1` at `NeewerLux.py:3057` and
      `NeewerLux.py:6981`. Both currently use `("", httpPort)`, which is all interfaces.
      Make LAN exposure an explicit opt-in in Global Preferences.
- [x] Replace the substring IP match at `NeewerLux.py:6238`
      (`if acceptable_HTTP_IPs[check] in clientIP`) with `ipaddress.ip_network()` and
      CIDR entries. The shipped default includes `"10."`, which matches `110.23.4.5`
      and `210.0.0.1`.
- [x] Refuse requests from browser origins that are not allowed, before the command
      runs. Withholding CORS headers does not stop a cross-origin GET; the browser sends
      it and the lights change regardless.
- [x] Validate the `Host` header against local addresses. Without it, DNS rebinding
      makes the browser report `Sec-Fetch-Site: same-origin` for an attacker's page.
- [ ] Consider a shared-secret token in the query string as defence in depth. Still the
      only thing that would close the residual gap: a browser too old to send
      `Sec-Fetch-Site` can drive an `<img src>` request that carries no `Origin` either.

## 4. Drop the PySide2 fallback

PySide2's final release is 5.15.2.1, capped at `<3.11`. It cannot coexist with current
bleak or PySide6 on any supported Python, so the fallback path is dead code.

- [x] Remove the PySide2 branch at `NeewerLux.py:91-96`.
- [x] Remove the `pyside_exec()` shim at `NeewerLux.py:169` and its call sites.
- [x] Remove `PYSIDE_VERSION` and its conditionals.

## 5. Tests and type hints

No test files exist. No function in any module carries an annotation.

- [ ] Add `ruff` and `pytest` via mise.
- [ ] Run both in CI on push, not only on tags. The release workflow is currently the
      only automation.
- [ ] Cover the pure functions first. `calculateChecksum`, `calculateByteString`,
      `convert_K_to_RGB`, `convert_HSI_to_RGB`, `interpolateHSI` (shortest-path hue
      wraparound regresses silently), and `parseBatchString` all have clear contracts
      and no I/O. Roughly twenty tests would catch the class of packaging regression
      that v1.2.0 spent its changelog restoring.
- [ ] Annotate as modules get touched rather than in one pass.

## 6. Replace the `availableLights` magic indices

Highest-value refactor in the codebase.

- [ ] Introduce a `@dataclass Light`. The schema currently exists only as a comment
      block at `NeewerLux.py:190-201`, and there are 123 subscript sites of the form
      `availableLights[i][3]`. Named attributes make every one of them readable and
      let a type checker find mistakes.

## 7. Split the 7,253-line module

`NeewerLux.py` is 399 KB holding the BLE protocol, the animation engine, the HTTP server
and its HTML, Qt window logic, and preference parsing.

- [ ] Extract `neewerlux/protocol.py` first. Bytestrings, checksums, and light specs are
      pure and testable.
- [ ] Extract `neewerlux/animation.py`.
- [ ] Extract `neewerlux/http_api.py`.
- [ ] Leave the Qt window last. Hardest to move, least rewarding.
- [ ] Collapse the ~48 module-level globals into a `Prefs` dataclass. They are mutated
      through `global` statements in 81 places. `NeewerLux.py:2269` lists
      `globalCCTMin`, `globalCCTMax`, `enableLogTab`, `logToFile`, and `cctFallbackMode`
      two to four times each in a single statement.

## 8. Move the custom config formats to TOML

There are currently **three** config formats in `light_prefs/`, two of them bespoke.

| File | Format today | Notes |
|------|-------------|-------|
| Global prefs | flat `key=value` | parsed by prefixing `--` and feeding to argparse |
| `customLights.prefs` | flat `key=value` with pipe-delimited values | e.g. `customPreset0=-1|5|90|42` |
| `NeewerLux.geometry` | JSON | already structured, fine as is |
| `animations/*.json` | JSON | already structured, content not config |

The global prefs loader at `NeewerLux.py:6798-6830` is the odd one. It reads the file,
filters lines against an `acceptable_arguments` list, prepends `--` to every surviving
line, then hands the result to `argparse`. Two problems fall out of that design:

- The filter is a substring match: `if not any(x in mainPrefs[a] for x in
  acceptable_arguments)`. Any line that happens to *contain* an accepted key name
  anywhere survives, including in a comment or a value.
- Everything arrives as a string. Types are recovered later by hand, which is why
  `testValid()` exists.

`customPreset0=-1|5|90|42` is the worse of the two. The pipe fields are positional with
no schema anywhere in the file, so the meaning of field 3 lives only in the reader code.

- [ ] Pick TOML over YAML. It is in the stdlib, has one obvious spelling for most
      things, and does not have YAML's implicit type coercion surprises (the Norway
      problem, sexagesimals, unquoted version strings). Nothing here needs anchors,
      references, or multi-document files.
- [ ] Note the stdlib boundary: `tomllib` (read) landed in **3.11**, not 3.10. If item 1
      pins `>=3.10` you also need `tomli` as a dependency. Pinning `>=3.11` instead
      avoids that, and both PySide6 and bleak allow it. Writing TOML always needs
      `tomli-w` regardless, since there is no stdlib writer.
- [ ] Merge global prefs and `customLights.prefs` into one `config.toml` with tables:
      `[general]`, `[http]`, `[hotkeys]`, `[cct]`, `[[presets]]`. Presets become an
      array of tables with **named** fields instead of positional pipe values.
- [ ] Delete the argparse-as-config-parser path entirely. Keep argparse for actual CLI
      flags only.
- [ ] Write a one-shot migration that reads either legacy file, writes `config.toml`,
      and renames the old file to `.prefs.bak`. Do not silently discard user presets.
- [ ] Do this **after item 6**. The `Prefs` dataclass is the natural deserialisation
      target, and doing both at once means debugging two changes in one diff.
- [ ] Leave `NeewerLux.geometry` as JSON. It is machine-written window state that no
      user edits by hand, so TOML buys nothing.

## 9. Docker release with signing and attestations

**Scope, decided: Linux only, REST server only, no GUI.** The image runs the existing
headless mode, `--http` (`NeewerLux.py:4959`, wrapped by `NeewerLux-HTTP.bat`), which
starts the HTTP server and the BLE worker thread with no Qt window. BLE reaches the
adapter through an external BlueZ D-Bus proxy rather than a host socket mount, so the
container does not need `--net=host` or privileged mode.

Two things still worth writing down in the README, since they are the first questions
anyone will ask:

- The image is headless. There is no GUI in it and there never will be. Name it so that
  is obvious (`neewerlux-server` or a `-server` tag suffix rather than plain
  `neewerlux`).
- It needs a reachable BlueZ D-Bus endpoint. Document the proxy setup and the
  `DBUS_SYSTEM_BUS_ADDRESS` value that points at it, because bleak will otherwise fail
  at adapter discovery with an error that does not explain itself.

Tasks:

- [ ] Write a `Dockerfile` targeting `--http` only. Multi-stage, `uv sync --frozen` from
      item 1.
- [ ] Keep PySide6 out of the runtime layer. It is the bulk of the image size and the
      headless path does not need a GUI toolkit. This needs a code change: the PySide6
      import at `NeewerLux.py:83` is unconditional at module scope, so today the `--http`
      path imports Qt even though it never opens a window. Making that import lazy is a
      small change with a large payoff here, and it speeds up `--cli` and `--list` too.
- [ ] Take `DBUS_SYSTEM_BUS_ADDRESS` from the environment so the proxy endpoint is
      configurable without rebuilding. Verify bleak honours it on the BlueZ backend
      before committing to the design.
- [ ] Add a healthcheck that hits the HTTP server rather than just checking the process,
      so a container that has lost its D-Bus connection reports unhealthy.
- [ ] Reconcile with item 3's localhost default. Binding `127.0.0.1` **inside** a
      container makes the port unreachable from the host even with `-p 8080:8080`, so the
      image needs `0.0.0.0` while the desktop app defaults to loopback. Drive it from one
      setting (env var or config key from item 8) and set it in the Dockerfile. Do not
      fix this by reverting item 3.
- [ ] Publish to GHCR on tag push, alongside the existing Windows job.
- [ ] Build `linux/amd64` and `linux/arm64`. Raspberry Pi is a plausible host for a
      headless light controller and the README already mentions RPi.
- [ ] Sign keyless with cosign via GitHub OIDC. No key material to manage or leak.
- [ ] Generate SLSA build provenance with `actions/attest-build-provenance`.
- [ ] Generate an SBOM and attest it too.
- [ ] Document the verification command (`cosign verify` with the workflow identity and
      `--certificate-oidc-issuer`). An unverifiable signature helps nobody.
- [ ] **Apply attestations to the existing Windows zip as well.** That artifact is what
      users actually download today, it is an unsigned executable, and attesting it is a
      few lines in the workflow you already have. Higher real-world value than the
      Docker image, and it is worth doing first.

## 10. Formatting

**Decided: `ruff format`, not Black.** Item 5 already adds ruff, and `ruff format` is a
Black-compatible reimplementation, so one tool covers linting and formatting from a
single config block. Black is not being added.

- [ ] Configure `[tool.ruff.format]` in `pyproject.toml` from item 1.
- [ ] Set line length deliberately. The default 88 will reformat a large fraction of this
      codebase, which has many long single-line strings and statements such as the
      `global` line at `NeewerLux.py:2269`. 100 or 120 produces a smaller diff and suits
      the existing style better.
- [ ] Format in **one dedicated commit that changes nothing else**, then record its SHA
      in `.git-blame-ignore-revs` and set `git config blame.ignoreRevsFile
      .git-blame-ignore-revs`. Without this, reformatting 10,294 lines makes `git blame`
      useless across the whole project. GitHub honours that file automatically.
- [ ] Sequence it **after item 4**. Reformatting the PySide2 fallback and then deleting
      it is wasted diff, and it makes the deletion commit harder to read.
- [ ] Sequence it **before items 6 and 7**. Refactor diffs stay readable when formatting
      noise is already out of the way.
- [ ] Add `--check` to CI so the tree cannot drift back.

## 11. HTTP `Accept: application/json` support

Confirmed still the case. There is **no `Accept` header inspection anywhere in the
request handler.** JSON support exists, but it is routed by path and by flag rather than
negotiated:

- `?list_json` returns JSON (`NeewerLux.py:6276`).
- `do_POST` always returns JSON (`NeewerLux.py:6674-6681`).
- Every `doAction?` command GET returns hand-built HTML through `writeHTMLSections()`,
  regardless of what the client asked for.
- `--nopage` (`NeewerLux.py:4969`, "don't render an HTML page") is an ad-hoc precursor to
  content negotiation and should be folded into it.
- Error responses are always HTML or plain text. A JSON client that hits the 403 IP
  check, the 1024-character URL limit, or an invalid-parameter error gets an HTML page
  and a parse failure.

Tasks:

- [x] Parse the `Accept` header once at the top of `do_GET` and set a `wantsJSON` flag.
      Treat `application/json` as JSON, `*/*` and everything else as HTML, so browsers
      keep working unchanged.
- [x] Return JSON from the `doAction?` command path when `wantsJSON` is set. Shape it
      like the existing POST responses (`{"success": bool, ...}`) so there is one
      response contract rather than two.
- [x] Convert the error paths too, including the 403 at `NeewerLux.py:6243` and the URL
      length limit at `NeewerLux.py:6222`. A machine client needs a parseable error more
      than a human needs a styled one.
- [x] Fold `--nopage` into the negotiation and keep the flag as a deprecated alias.
- [x] Keep `?list_json` working as an alias for `?list` with `Accept: application/json`.
      Deprecate it in the docs but do not break the web dashboard, which calls it.
- [x] Do this **alongside item 3**. Both rewrite the same handler, and item 3's error
      responses want the JSON shape defined here.
- [x] Minor cleanup while in there: `NeewerLux.py:6314` does `import json as _json` inside
      the function when `json` is already imported at module level (`NeewerLux.py:33`).

## 12. Renovate

**Sequence this after item 1.** Renovate has almost nothing to scan today: there is no
`pyproject.toml`, no lockfile, and no `requirements.txt`, so the only manifest in the
repo is `.github/workflows/release.yml`. Enabling it now would produce action-version PRs
and nothing else, and the unpinned `pip install PySide6 bleak pyinstaller` at
`release.yml:30` is invisible to it. Once item 1 lands, Renovate becomes genuinely
useful.

What it will manage once the manifests exist:

| Target | Appears after |
|--------|--------------|
| `pyproject.toml` and `uv.lock` | item 1 |
| GitHub Actions versions in `release.yml` | already present |
| `.mise/config.toml` tool versions | already present, Renovate has native mise support |
| `.devcontainer/devcontainer.json` features and `devcontainer-lock.json` | already present |
| Dockerfile base image | item 9 |

Tasks:

- [ ] Install the Renovate GitHub App and add `renovate.json` extending
      `config:recommended`.
- [ ] Enable the lockfile maintenance preset so `uv.lock` gets refreshed on a schedule
      rather than only when a constraint changes.
- [ ] Group the PySide6 packages together. They release as a set and separate PRs for
      each will be noise.
- [ ] Set a schedule. Default Renovate on a solo project produces more PRs than it is
      worth reviewing. Weekly, with a concurrent PR limit, is usually the right setting.
- [ ] Turn on `vulnerabilityAlerts` so CVE-driven bumps bypass the schedule and the PR
      limit. That is the one case where you want the PR immediately.
- [ ] Require CI green before automerge, and only automerge dev dependencies and patch
      bumps. **Do not automerge PySide6 or bleak.** Both sit on the critical path of a
      GUI and a BLE stack that CI cannot meaningfully exercise without hardware, so a
      green build proves very little about them.
- [ ] Depends on item 5's CI. Renovate without a test suite running on PRs is just an
      automated way to break `main`.

Adjacent and free, worth enabling at the same time even though they are not Renovate:
Dependabot **alerts** (advisory notifications, distinct from Dependabot version PRs,
which Renovate replaces), secret scanning with push protection, and CodeQL for Python.
All three are repository settings toggles.

## 13. Remove Qt entirely

Supersedes item 4, which removed only the dead PySide2 branch from files that are now
being deleted outright.

- [ ] Delete `neewerlux_ui.py` and `neewerlux_anim_editor.py`.
- [ ] Delete the `MainWindow` class and the GUI startup block from `NeewerLux.py`.
- [ ] Extract `_savePresetsQuick` to a module-level function before deleting the class.
      It is called from three places outside it.
- [ ] Strip the Qt stylesheet from `neewerlux_theme.py`, keep the web CSS variables.
- [ ] Drop the `gui` dependency group from `pyproject.toml` and `default-groups`.
- [ ] Delete `NeewerLux.bat` (GUI launcher) and the `.desktop` entry. Keep the HTTP one.
- [ ] Rework `NeewerLux.spec` for a console application, or drop PyInstaller if the
      Windows executable no longer makes sense for a headless service.
- [ ] Rewrite the README. It currently sells a desktop GUI to streamers.
- [ ] Decide what replaces the removed editors, if anything. Animations and presets are
      already plain files; the web dashboard could grow forms later, but nothing is
      blocked without them.

---

## Loose bug

- [x] `NeewerLux.py:7149` does `workerThread = threading.Thread(target=workerThread, ...)`,
      rebinding the module-level function name to a Thread object. It works exactly once.
      Any path reaching it a second time raises `TypeError: 'Thread' object is not
      callable`. Rename the local. Independent of everything above.
