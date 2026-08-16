"""Python sidecar for the Tauri desktop installer.

Reads wizard steps from stdin, writes JSON-formatted next-step
descriptors to stdout. The Tauri Rust shell drives this process and
the Svelte UI sends user answers via Tauri invoke calls.

Protocol (line-delimited JSON):

  stdin:  <user_answer_string>  (empty string for first call)
  stdout: {"id": "<step_id>", "question": "...", "choices": ["...", ...]}

When the wizard is complete, sidecar emits ``{"id": "__done__", ...}``
and exits.

The five questions mirror the CLI's consumer mode exactly (name,
governance level, sign-in key, working directory, budget) and the
resulting config is written by the SAME ``write_consumer_config``
helper the CLI uses -- and the governance menu is imported from the
SAME ``GOVERNANCE_CHOICES`` list the CLI shows -- so the GUI and CLI
can't drift.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from .wizard import (
    DEFAULT_GOVERNANCE_PROFILE,
    GOVERNANCE_CHOICES,
    GOVERNANCE_PROFILES,
    write_consumer_config,
)


def _step(id: str, question: str, choices: list[str], *, kind: str = "choice") -> dict[str, Any]:
    # `kind` lets the Svelte UI render the right control: a choice list,
    # a free-text box, or a masked secret field.
    return {"id": id, "question": question, "choices": choices, "kind": kind}


def _send(step: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(step) + "\n")
    sys.stdout.flush()


def _recv() -> str:
    return sys.stdin.readline().strip()


def run() -> None:
    """Sidecar entry point: the four-question consumer flow.

    Matches ``maverick_installer.wizard.run_consumer`` question for
    question. No jargon, no provider picker, no DevTools paste.
    """
    default_name = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    default_workdir = str(Path.home() / "Documents" / "Maverick")

    # The first invoke carries no answer; consume it and ask Q1.
    _recv()

    # Q1: name
    _send(_step(
        "name",
        "What should we call you?",
        [],
        kind="text",
    ))
    user_name = _recv().strip() or default_name or "you"

    # Q2: governance level. The menu is wizard.GOVERNANCE_CHOICES verbatim --
    # the CLI's list is the UX source of truth; do not re-author it here.
    # Resolution is deliberately strict: a BLANK answer means "skipped" and
    # takes the default, but a non-blank answer that names no known level is
    # a protocol bug in the UI and must fail LOUDLY. Silently falling back
    # would downgrade a bank that picked "regulated" to the least-governed
    # tier with no error -- the one failure direction this selector must
    # never have.
    _send(_step(
        "governance",
        "What kind of business is this for? Every level learns and "
        "improves; the level sets how much governance wraps it.",
        list(GOVERNANCE_CHOICES),
    ))
    raw_answer = _recv().strip()
    if not raw_answer:
        profile = DEFAULT_GOVERNANCE_PROFILE
    else:
        first = raw_answer.split()[0].lower()
        profile = first if first in GOVERNANCE_PROFILES else next(
            (name for name in GOVERNANCE_PROFILES if name in raw_answer.lower()),
            "",
        )
        if not profile:
            raise ValueError(
                f"unrecognized governance level answer: {raw_answer!r} "
                f"(expected one of {sorted(GOVERNANCE_PROFILES)})"
            )

    # Q3: API key (sign-in). The UI shows the console.anthropic.com link
    # and a masked field. Blank == skip (config saved without a key).
    _send(_step(
        "api_key",
        "Paste your Anthropic API key. Get one at "
        "https://console.anthropic.com/settings/keys (or leave blank to skip).",
        [],
        kind="secret",
    ))
    raw_key = _recv().strip()
    keys: dict[str, str] = {"ANTHROPIC_API_KEY": raw_key} if raw_key else {}

    # Q4: working directory
    _send(_step(
        "workdir",
        "Where can Maverick work? (a folder it can create files in)",
        [],
        kind="text",
    ))
    workdir = _recv().strip() or default_workdir

    # Q5: budget
    _send(_step(
        "budget",
        "Stop after spending how much per task?",
        ["$1", "$5", "$20"],
    ))
    budget_ans = _recv().strip() or "$5"
    try:
        dollars = float(budget_ans.lstrip("$"))
    except ValueError:
        dollars = 5.0
    budget = {
        "max_dollars": dollars,
        "max_wall_seconds": 600.0,
        "max_tool_calls": 100,
    }

    write_consumer_config(
        user_name=user_name, keys=keys, workdir=workdir, budget=budget,
        profile=profile,
    )

    if keys:
        msg = f"Setup complete, {user_name}. Maverick is ready."
    else:
        msg = (
            f"Setup saved, {user_name}. Add an API key later from Settings "
            "or by running 'maverick init' again."
        )
    _send(_step("__done__", msg, []))


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        _send({"id": "__error__", "question": str(e), "choices": [], "kind": "error"})
        sys.exit(1)
