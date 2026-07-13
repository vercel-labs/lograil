# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Composable log entry remaps."""

from __future__ import annotations

from typing import Any, Protocol

from collections.abc import Iterable

from lograil._internal import progress

LogEntry = dict[str, Any]

PROGRESS_DESCRIPTION = "lograil.progress.description"
PROGRESS_COMPLETED = "lograil.progress.completed"
PROGRESS_TOTAL = "lograil.progress.total"
PROGRESS_LABEL = "lograil.progress.label"
PROGRESS_PROCESS = "lograil.progress.process"
PROGRESS_SUBJECT = "lograil.progress.subject"
PROGRESS_CLEAR_LABEL = "lograil.progress.clear_label"
STATUS_ONLY = "lograil.status_only"


class Remap(Protocol):
    """Callable that transforms or drops one log entry."""

    def __call__(self, entry: LogEntry) -> LogEntry | None:
        """Return a transformed entry, or ``None`` to drop it."""


class RemapPipeline:
    """Apply log entry remaps in order.

    Each :class:`Remap` receives the current entry and returns a
    transformed entry, or ``None`` to drop it (short-circuiting the
    rest of the pipeline).  The input entry is shallow-copied first, so
    remaps may mutate their argument freely.  ``DEFAULT_REMAPS``
    normalizes level/message fields and extracts structured progress
    metadata.
    """

    def __init__(self, remaps: Iterable[Remap]) -> None:
        self._remaps = tuple(remaps)

    def __call__(self, entry: LogEntry) -> LogEntry | None:
        """Apply configured remaps to a shallow copy of ``entry``."""
        current = dict(entry)
        for remap in self._remaps:
            mapped = remap(current)
            if mapped is None:
                return None
            current = mapped
        return current


def normalize_entry(entry: LogEntry) -> LogEntry:
    """Normalize generic lograil fields."""
    if "message" not in entry and "msg" in entry:
        entry["message"] = str(entry["msg"])
    if "levelname" in entry:
        entry["levelname"] = str(entry["levelname"]).upper()
    elif "level" in entry:
        entry["levelname"] = str(entry["level"]).upper()
    else:
        entry["levelname"] = "INFO"
    return entry


def extract_progress_metadata(entry: LogEntry) -> LogEntry:
    """Annotate entries containing structured progress lines.

    The raw ``::lograil-progress::`` IPC line is replaced with the
    update's description so no output mode leaks the wire syntax; the
    structured fields ride along under the ``lograil.progress.*`` keys.
    """
    msg = entry.get("message")
    if not isinstance(msg, str):
        return entry
    update = progress.parse(msg)
    if update is None:
        return entry
    entry["message"] = update.description
    if entry.get("lograil.status.detail") == msg:
        entry["lograil.status.detail"] = update.description
    entry[PROGRESS_DESCRIPTION] = update.description
    entry[PROGRESS_COMPLETED] = update.completed
    entry[PROGRESS_TOTAL] = update.total
    if update.label is not None:
        entry[PROGRESS_LABEL] = update.label
    if update.process is not None:
        entry[PROGRESS_PROCESS] = update.process
    if update.subject is not None:
        entry[PROGRESS_SUBJECT] = update.subject
    if update.clear_label:
        entry[PROGRESS_CLEAR_LABEL] = True
    return entry


DEFAULT_REMAPS: tuple[Remap, ...] = (
    normalize_entry,
    extract_progress_metadata,
)
