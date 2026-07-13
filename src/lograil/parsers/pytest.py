# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Pytest process output parser."""

from __future__ import annotations

from lograil._internal import remap
from lograil._internal.tail import LogEntry


class PytestOutputParser:
    """Stateful per-process parser for pytest terminal output."""

    def __init__(self) -> None:
        """Create a parser with isolated per-process progress state."""
        self._total: int | None = None
        self._completed = 0
        self._seen_nodeids: set[str] = set()

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate ``entry`` with pytest status and progress fields."""
        message = entry.get("message")
        if not isinstance(message, str):
            return entry
        entry["lograil.status.detail"] = ""
        line = message.strip()
        if line.startswith("collecting"):
            entry["lograil.status.detail"] = line
            return entry
        if line.startswith("collected ") and " item" in line:
            entry["lograil.status.detail"] = line
            total = _first_int(line.removeprefix("collected "))
            if total is not None:
                self._total = total
                entry[remap.PROGRESS_DESCRIPTION] = "pytest"
                entry[remap.PROGRESS_COMPLETED] = 0
                entry[remap.PROGRESS_TOTAL] = total
            return entry
        if _is_xdist_startup(line):
            entry["lograil.status.detail"] = line
            return entry
        if " workers [" in line and " item" in line:
            entry["lograil.status.detail"] = line
            total = _xdist_total(line)
            if total is not None:
                self._total = total
                entry[remap.PROGRESS_DESCRIPTION] = "pytest"
                entry[remap.PROGRESS_COMPLETED] = 0
                entry[remap.PROGRESS_TOTAL] = total
            return entry
        nodeid = _pytest_nodeid(line)
        if nodeid is None:
            if self._total is not None and _is_compact_progress_line(line):
                self._completed = max(
                    self._completed,
                    min(self._completed + len(line), self._total),
                )
                entry["lograil.status.detail"] = "pytest"
                entry[remap.PROGRESS_DESCRIPTION] = "pytest"
                entry[remap.PROGRESS_COMPLETED] = self._completed
                entry[remap.PROGRESS_TOTAL] = self._total
            return entry
        entry["lograil.status.detail"] = nodeid
        percent = _pytest_percent(line)
        if percent is not None:
            completed, total = self._progress_from_percent(percent)
            self._completed = max(self._completed, completed)
            entry[remap.PROGRESS_DESCRIPTION] = "pytest"
            entry[remap.PROGRESS_COMPLETED] = self._completed
            entry[remap.PROGRESS_TOTAL] = total
        elif self._total is not None:
            self._seen_nodeids.add(nodeid)
            self._completed = max(
                self._completed,
                min(len(self._seen_nodeids), self._total),
            )
            entry[remap.PROGRESS_DESCRIPTION] = "pytest"
            entry[remap.PROGRESS_COMPLETED] = self._completed
            entry[remap.PROGRESS_TOTAL] = self._total
        return entry

    def _progress_from_percent(self, percent: int) -> tuple[int, int]:
        if self._total is None:
            return percent, 100
        completed = min(self._total, round(self._total * percent / 100))
        return completed, self._total


def _first_int(value: str) -> int | None:
    token = value.split(maxsplit=1)[0]
    try:
        return int(token)
    except ValueError:
        return None


def _xdist_total(line: str) -> int | None:
    try:
        raw = line.rsplit("[", 1)[1].split(" item", 1)[0].strip()
        return int(raw)
    except (IndexError, ValueError):
        return None


def _is_xdist_startup(line: str) -> bool:
    return line.startswith(("created: ", "scheduling tests via "))


def _pytest_nodeid(line: str) -> str | None:
    # The nodeid is not necessarily the first token: summary lines such
    # as "FAILED tests/x.py::test_y - assert False" lead with the outcome.
    for token in line.split():
        if "::" in token:
            return token
    return None


def _pytest_percent(line: str) -> int | None:
    if "[" not in line or "%" not in line:
        return None
    raw = line.rsplit("[", 1)[-1].split("%", 1)[0].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _is_compact_progress_line(line: str) -> bool:
    return bool(line) and all(char in ".FEfsxXP" for char in line)
