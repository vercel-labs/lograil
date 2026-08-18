# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from lograil._internal.env import detect_agent


@pytest.mark.parametrize(
    ("expected", "marker"),
    [
        ("claude", "CLAUDECODE"),
        ("claude", "CLAUDE_CODE"),
        ("codex", "CODEX_SANDBOX"),
        ("codex", "CODEX_THREAD_ID"),
        ("codex", "CODEX_CI"),
        ("gemini", "GEMINI_CLI"),
        ("opencode", "OPENCODE"),
        ("cursor", "CURSOR_AGENT"),
        ("auggie", "AUGMENT_AGENT"),
        ("junie", "JUNIE_DATA"),
        ("junie", "JUNIE_SHIM_PATH"),
    ],
)
def test_detect_agent_from_session_marker(expected: str, marker: str) -> None:
    assert detect_agent({marker: "1"}, is_tty=True) == expected


def test_detect_agent_from_explicit_name() -> None:
    assert detect_agent({"AI_AGENT": " Custom-Agent "}) == "custom-agent"


@pytest.mark.parametrize(
    "path",
    [
        "/usr/bin:/Users/example/.pi/agent/bin",
        r"C:\Users\example\.pi\agent\bin;C:\Windows",
    ],
)
def test_detect_pi_from_agent_path(path: str) -> None:
    assert detect_agent({"PATH": path}) == "pi"


def test_detect_kiro_only_without_tty() -> None:
    env = {"TERM_PROGRAM": "kiro"}

    assert detect_agent(env, is_tty=False) == "kiro"
    assert detect_agent(env, is_tty=True) is None


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"AI_AGENT": ""},
        {"CODEX_THREAD_ID": ""},
        {"PATH": "/usr/bin:/tmp/not.pi/agentic/bin"},
        {"REPL_ID": "hosted-environment"},
        {"GOOSE_PROVIDER": "configured-provider"},
    ],
)
def test_detect_agent_ignores_weak_or_empty_signals(
    env: dict[str, str],
) -> None:
    assert detect_agent(env) is None
