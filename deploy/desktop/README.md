# Desktop deployment

## Terminal install

The Maverick distribution names are not yet reserved on public PyPI. Do not
install those names from the public index. Use a reviewed source commit:

```bash
export MAVERICK_REF=<full-40-character-commit-sha>
bash deploy/desktop/install.sh
```

On Windows, set `$env:MAVERICK_REF` and run `deploy/desktop/install.ps1`.
Both scripts fail before changing the machine when the ref is missing or
mutable, and neither has a public-index fallback.

## Source bootstrap (advanced)

Download `install.sh` or `install.ps1` from the same commit you intend to
install, verify the script, and set `MAVERICK_REF` to that lowercase, full
40-character commit SHA before running it. Mutable branch/tag refs are always
rejected.

## Native bundles (planned)

The long-term plan ships native bundles per platform so users don't
need Python installed at all:

| Platform | Tool | Format | Auto-update |
|---|---|---|---|
| macOS | Tauri | Notarized DMG, signed `.app` | Sparkle via Tauri updater |
| Windows | Tauri | Signed MSIX | Tauri updater |
| Linux | Tauri | AppImage + `.deb` + `.rpm` | AppImageUpdate |

The Tauri shell ships an embedded Python runtime via
[PyOxidizer](https://pyoxidizer.readthedocs.io/) or
[python-build-standalone](https://github.com/indygreg/python-build-standalone),
and the wizard runs as a Svelte UI talking to a sidecar Python process.

See [`apps/installer-desktop/`](../../apps/installer-desktop/README.md)
for the scaffold and milestones.
