# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Runtime environment detection."""

from __future__ import annotations

from typing import TYPE_CHECKING

import os
import re

if TYPE_CHECKING:
    from collections.abc import Mapping


_AGENT_ENV_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("claude", ("CLAUDECODE", "CLAUDE_CODE")),
    ("codex", ("CODEX_SANDBOX", "CODEX_THREAD_ID", "CODEX_CI")),
    ("gemini", ("GEMINI_CLI",)),
    ("opencode", ("OPENCODE",)),
    ("cursor", ("CURSOR_AGENT",)),
    ("auggie", ("AUGMENT_AGENT",)),
    ("junie", ("JUNIE_DATA", "JUNIE_SHIM_PATH")),
)
_PI_AGENT_PATH = re.compile(r"(?:^|[\\/])\.pi[\\/]agent(?:[\\/]|$)")


def detect_agent(
    env: Mapping[str, str] | None = None, *, is_tty: bool = False
) -> str | None:
    """Return the detected coding agent name, if any.

    Detection intentionally uses only session-scoped markers. Broad signals
    that can also describe a hosted environment or persistent configuration
    are excluded to avoid changing output for human-driven terminal sessions.
    """
    environment = os.environ if env is None else env

    if explicit := environment.get("AI_AGENT", "").strip():
        return explicit.lower()

    for agent, markers in _AGENT_ENV_MARKERS:
        if any(environment.get(marker) for marker in markers):
            return agent

    if _PI_AGENT_PATH.search(environment.get("PATH", "")):
        return "pi"

    if not is_tty and environment.get("TERM_PROGRAM", "").lower() == "kiro":
        return "kiro"

    return None
