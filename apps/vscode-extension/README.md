# Maverick for VS Code

Sidebar + commands for the [Maverick](https://github.com/Daybreak-AI-Labs/Law_Firm)
agent framework, accessible from inside VS Code.

This is an MVP (v0.1):

- Sidebar **Maverick** view with a recent-runs summary.
- Commands: **Start goal**, **Show status**, **Halt**, **Unhalt**,
  **Export run as JSON**, **Refresh runs**.
- All commands shell out to the user's local `maverick` CLI. No
  daemon, no embedded Python.

## Setup

1. Install Maverick from a reviewed source commit:
   `pip install -e ./packages/maverick-core` (then run `maverick init`). Do
   not install the unreserved distribution name from public PyPI.
2. Build the extension: `cd apps/vscode-extension && npm install && npm run compile`.
3. From VS Code: **Run** → **Run Extension** (F5), or package with
   `vsce package` and install the `.vsix`.

## Config

| Setting               | Default     | Description                                      |
|-----------------------|-------------|--------------------------------------------------|
| `maverick.cliPath`    | `maverick`  | Path to the `maverick` CLI executable.           |
| `maverick.workspaceCwd` | `true`    | Use the current workspace as cwd when running.   |

## Not built yet

- Live run streaming via the `maverick serve` REST API.
- Plan-tree visualization.
- Approve-tool-call inline in the editor.
- Right-click context: send the selection to the platform.

## License

Proprietary — same as the main project; see [`LICENSE`](../../LICENSE).
