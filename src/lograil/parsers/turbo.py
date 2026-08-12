# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Turbo build process output parser."""

from __future__ import annotations

import re

from lograil._internal import remap
from lograil._internal.tail import LogEntry

_TASK_LINE_RE = re.compile(r"^(.+?):([^:]+): (.*)$")


class TurboOutputParser:
    """Annotate Turbo cache-status lines with task progress."""

    def __init__(self, *, total_tasks: int | None = None) -> None:
        """Create a parser, optionally with a dry-run task total."""
        self.total_tasks = total_tasks
        self.started_tasks: set[str] = set()
        self._captured_output: list[str] = []

    @property
    def captured_output(self) -> str:
        """Every raw line observed by this parser."""
        return "\n".join(self._captured_output)

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate one Turbo output entry."""
        message = entry.get("message")
        if not isinstance(message, str):
            return entry
        self._captured_output.append(message)
        match = _TASK_LINE_RE.match(message)
        if match is None or not match.group(3).startswith("cache "):
            return entry

        package, task_name = match.group(1), match.group(2)
        task_id = f"{package}:{task_name}"
        if task_id in self.started_tasks:
            return entry
        self.started_tasks.add(task_id)
        entry["lograil.status.detail"] = package
        entry[remap.PROGRESS_DESCRIPTION] = package
        entry[remap.PROGRESS_COMPLETED] = len(self.started_tasks)
        if self.total_tasks is not None:
            entry[remap.PROGRESS_TOTAL] = self.total_tasks
        return entry
