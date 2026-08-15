# Lightwork authenticated source bootstrap

Tauri GUI bootstrap for authorized repository users who prefer not to drive
the source install from a terminal. A double-click app with one button: it runs
the same bootstrap the CLI
one-liners use (`deploy/desktop/install.{ps1,sh}`) behind a live
progress screen.

This is not a self-contained native installer. It clones a pinned commit from
the private Lightwork repository, so network access and valid GitHub
authorization are required, and the installed Python source remains readable.

## How it works

```
  +------------------+   invoke('install')    +-------------------+
  | Svelte UI (TS)   | ────────────────────► | Rust shell (lib)  |
  |  Install button  | ◄───── events ──────── |  spawns bootstrap |
  +------------------+  install-log / -done    +---------+---------+
                                                          │
                                                          │ stdout/stderr
                                                          ▼
                                          deploy/desktop/install.{ps1,sh}
                                          (MAVERICK_NO_WIZARD=1):
                                          installs Python + git if needed,
                                          then pipx-installs Lightwork
```

The Rust shell owns the window and runs the bootstrap as a subprocess,
streaming each line to the UI as a Tauri event. **No Python is required
on the machine first** — the bootstrap installs it. This is why we shell
out to the existing scripts instead of embedding a Python runtime: those
scripts are already tested and are the single source of truth for "how
to install" (winget/brew/apt, PATH, pipx, PEP 668). When the bootstrap
finishes, the UI tells the user to run `maverick init` to configure.

## Status

Builds **unsigned engineering workflow artifacts** on macOS / Windows / Linux
via `.github/workflows/desktop.yml`. The workflow verifies every raw source
byte against the triggering canonical Lightwork commit before running package
scripts, and the Rust build script repeats that gate before embedding the
committed bootstrap blobs. Git replacement refs and inherited `GIT_*` routing
variables are disabled for those checks.

Each uploaded artifact contains:

- a `.tar.gz` bundle package, so executable modes and symbolic-link metadata
  survive GitHub artifact transport; and
- a machine-readable provenance manifest with the repository/workflow/run
  identity, exact target, effective version override, committed bootstrap and
  icon/lock/config hashes, archive checksum, and bundle-entry checksums.

The upload paths are exact rather than wildcarded, and cached bundle output is
deleted before every build so a prior version cannot be mislabeled as the
current revision. These artifacts are still unsigned and are not attached to
product releases. A consumer-grade installer requires a signed-asset flow,
platform code signing, and real-machine install tests.

> **Needs real-machine testing.** CI confirms the bundle *builds*, but
> the actual install run (winget/brew, network, the GUI driving the
> bootstrap) has to be exercised on real Windows/macOS/Linux. Treat the
> first build as a release candidate to smoke-test, not a shipped
> artifact.

Cancellation owns and reaps the bootstrap process group on Unix and a
KILL_ON_JOB_CLOSE Job Object on Windows before the UI reports success. Package
managers or elevation brokers may cross that ordinary ownership boundary, and
an interrupted package operation may leave a partial dependency install.
Real-machine release testing must cover winget/brew/apt and elevation flows;
after cancellation, use the safe retry path (or the platform package manager's
repair/uninstall command) rather than assuming the machine was rolled back.

The Rust lockfile is current for Tauri 2.11, but its Linux dependency graph
still inherits Wry's GTK3 bindings. OSV Scanner 2.3.8 reports one
medium-severity `glib` soundness advisory and sixteen unmaintained-crate
advisories. They have no compatible lockfile-only resolution: current Tauri
requires GTK/GLib 0.18, the `glib` fix begins at 0.20, and most of the
maintenance advisories have no fixed release. The GLib/GTK-family findings are
Linux-only. Five additional `unic-*` maintenance advisories arrive through
Tauri's cross-platform `urlpattern` dependency; they describe abandoned crates
rather than known exploitable defects and also have no fixed versions. Keep
Linux bundles engineering-only, do not globally suppress the findings, and
re-evaluate when Tauri/Wry adopts maintained dependencies.

## Local development

```bash
cd apps/installer-desktop
pnpm install
pnpm tauri dev
```

Hot-reloads the Svelte frontend. Clicking **Install from authorized source**
runs the bundled
bootstrap, so test in a throwaway VM/container unless you actually want
Lightwork installed on your dev box. The desktop app embeds the installer
script blob directly from the git commit captured at build time and reports
that blob's SHA-256 in the install log. It pins the installed source to the
same commit; set
`MAVERICK_INSTALL_REF=<lowercase-full-40-character-commit-sha>` while building
only when you need to select an ancestor of the current commit intentionally.
Mutable tags, branches, unrelated commits, non-canonical repository origins,
Git replacement refs, and inherited Git object-routing variables are rejected
or disabled by the build. Release-grade bundle CI additionally byte-compares
the full installer source tree and both desktop bootstrap scripts with the
commit, including ignored and untracked files outside explicit generated
directories.

The five icon variants referenced by `tauri.conf.json` are committed build
inputs. If the source icon changes, regenerate the variants locally with
`pnpm tauri icon src-tauri/icons/icon.png`, review all six binary changes, and
commit them together. Bundle CI never regenerates or silently rewrites tracked
icons.

Before opening a pull request, run the same frontend checks as CI:

```bash
pnpm check
pnpm test
pnpm build
```

## Producing engineering source-bootstrap bundles

```bash
pnpm tauri build
```

The build script records `git rev-parse HEAD` as the install ref, so the GUI
installer does not fetch and execute a mutable `main` branch bootstrap at
install time. Workflow tag versions are passed through a temporary Tauri
configuration override after strict SemVer validation; tracked
`tauri.conf.json` is never rewritten.

Engineering outputs per platform:
- macOS: `.app` + `.dmg` (sign + notarize for distribution)
- Windows: `.msi` + `.exe` (NSIS)
- Linux: `.AppImage` + `.deb`

## Why Tauri vs Electron

| | Tauri | Electron |
|---|---|---|
| Bundle size | ~5 MB | ~150 MB |
| Memory at idle | ~50 MB | ~250 MB |
| Native webview | system (WebKit / WebView2 / WebKitGTK) | bundled Chromium |
| Rust shell | yes (security, smaller attack surface) | no |

Lightwork already needs Rust in the toolchain for the agent-shield
performance core, so adding Tauri is essentially free.
