# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Structured progress lines emitted by child processes or log streams."""

from __future__ import annotations

from typing import Any, cast

import json
import math
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from rich.markup import escape as _escape_markup
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
)
from rich.text import Text

from lograil._internal import console, log

PROGRESS_LINES_ENV = "LOGRAIL_PROGRESS_LINES"
_PROGRESS_LINES_ENV = PROGRESS_LINES_ENV
_PROGRESS_LINE_PREFIX = "::lograil-progress::"
PROGRESS_BAR_WIDTH = 20
"""Width, in cells, of rendered progress bars."""
_PROGRESS_BAR_WIDTH = PROGRESS_BAR_WIDTH
_PROGRESS_PERCENT_WIDTH = 4
_PROGRESS_FIXED_WIDTH = 3 + _PROGRESS_BAR_WIDTH + _PROGRESS_PERCENT_WIDTH
_STATUS_SPINNER_OVERHEAD = 3
_PROGRESS_COMPLETE_GLYPH = "━"
_PROGRESS_REMAINING_GLYPH = "─"
_PROGRESS_ASCII_COMPLETE_GLYPH = "="
_PROGRESS_ASCII_REMAINING_GLYPH = "-"


@dataclass(frozen=True)
class ProgressUpdate:
    """One structured progress update.

    ``description`` names the current work item; ``completed``/``total``
    drive the bar.  A ``None`` ``total`` marks indeterminate progress:
    no bar or percentage is rendered, only the description.  ``label``
    (or the structured ``process``/``subject`` pair) titles the bar;
    ``clear_label`` tears the bar down and restores the plain status
    spinner.  Instances are parsed from child-process progress lines
    (:func:`parse`) or built from entry metadata via
    :meth:`from_mapping`.
    """

    description: str
    completed: int
    total: int | None = None
    label: str | None = None
    process: str | None = None
    subject: str | None = None
    clear_label: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> ProgressUpdate | None:
        """Validate generic progress metadata from a mapping."""
        description = data.get("description")
        completed = data.get("completed")
        total = data.get("total")
        label = data.get("label")
        process = data.get("process")
        subject = data.get("subject")
        clear_label = data.get("clear_label", False)
        if not isinstance(description, str):
            return None
        if not isinstance(completed, int):
            return None
        if total is not None and not isinstance(total, int):
            return None
        if label is not None and not isinstance(label, str):
            return None
        if process is not None and not isinstance(process, str):
            return None
        if subject is not None and not isinstance(subject, str):
            return None
        if not isinstance(clear_label, bool):
            return None
        return cls(
            description=description,
            completed=completed,
            total=total,
            label=label,
            process=process,
            subject=subject,
            clear_label=clear_label,
        )


def lograil_instrumentation_env() -> Mapping[str, str]:
    """Return env vars that enable lograil child instrumentation."""
    return {_PROGRESS_LINES_ENV: "1"}


def should_emit_progress_lines() -> bool:
    """Return whether child code should emit parent-readable progress."""
    return os.environ.get(_PROGRESS_LINES_ENV) == "1"


def format_line(
    *,
    description: str,
    completed: int,
    total: int | None = None,
    label: str | None = None,
    process: str | None = None,
    subject: str | None = None,
    clear_label: bool = False,
    prefix: str = _PROGRESS_LINE_PREFIX,
) -> str:
    """Return one structured progress update line."""
    payload: dict[str, object] = {
        "description": description,
        "completed": completed,
    }
    if total is not None:
        payload["total"] = total
    if label is not None:
        payload["label"] = label
    if process is not None:
        payload["process"] = process
    if subject is not None:
        payload["subject"] = subject
    if clear_label:
        payload["clear_label"] = True
    return prefix + json.dumps(payload, sort_keys=True)


def emit_line(line: str) -> None:
    """Write one preformatted structured progress update to stdout."""
    print(line, flush=True)  # ruff:ignore[print] - subprocess IPC via stdout.


def emit(
    *,
    description: str,
    completed: int,
    total: int | None = None,
    label: str | None = None,
    process: str | None = None,
    subject: str | None = None,
    clear_label: bool = False,
    prefix: str = _PROGRESS_LINE_PREFIX,
) -> None:
    """Write one structured progress update to stdout."""
    emit_line(
        format_line(
            description=description,
            completed=completed,
            total=total,
            label=label,
            process=process,
            subject=subject,
            clear_label=clear_label,
            prefix=prefix,
        )
    )


def parse(
    line: str,
    *,
    prefix: str = _PROGRESS_LINE_PREFIX,
) -> ProgressUpdate | None:
    """Parse a structured progress line from subprocess output."""
    matched_prefix = prefix if line.startswith(prefix) else None
    if matched_prefix is None:
        return None
    try:
        payload = json.loads(line[len(matched_prefix) :])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    data = cast("dict[str, Any]", payload)
    return ProgressUpdate.from_mapping(data)


class _PaddedPercentColumn(ProgressColumn):
    """Progress column showing a fixed-width percentage."""

    def render(self, task: Any) -> Text:
        if task.total is None:
            return Text(" ??%", style="progress.percentage")
        percentage = math.floor(task.percentage)
        if task.total and task.completed >= task.total:
            percentage = 100
        return Text(f"{percentage:>3.0f}%", style="progress.percentage")


def _progress_description_width(description: str) -> int:
    return Text.from_markup(description).cell_len


def _progress_detail_width(description: str) -> int:
    available = (
        console.stderr_console.width
        - _progress_description_width(description)
        - _PROGRESS_FIXED_WIDTH
    )
    return max(1, available)


def _status_progress_detail_width(
    description: str, *, with_bar: bool = True
) -> int:
    available = (
        console.stderr_console.width
        - _STATUS_SPINNER_OVERHEAD
        - _progress_description_width(description)
        - (_PROGRESS_FIXED_WIDTH if with_bar else 1)
    )
    return max(1, available)


def _format_status_progress(
    *,
    prefix: str | None = None,
    description: str | None = None,
    detail: str,
    completed: int,
    total: int | None,
) -> Text:
    if prefix is None:
        if description is None:
            raise ValueError("status progress requires prefix or description")
        prefix = description
    text = Text()
    if total is not None:
        text.append_text(
            render_progress_bar(
                completed=completed,
                total=total,
                width=_PROGRESS_BAR_WIDTH,
            )
        )
        pct = progress_percent(completed=completed, total=total)
        text.append(f" {pct:>3d}% ", style="progress.percentage")
    detail_text = Text.from_markup(_escape_markup(detail))
    detail_width = _status_progress_detail_width(
        prefix, with_bar=total is not None
    )
    if detail_text.cell_len > detail_width:
        detail_text.truncate(detail_width, overflow="ellipsis")
    detail_text.stylize("dim")
    text.append_text(detail_text)
    return text


def _progress_bar_glyphs() -> tuple[str, str]:
    if console.stderr_console.legacy_windows:
        return _PROGRESS_ASCII_COMPLETE_GLYPH, _PROGRESS_ASCII_REMAINING_GLYPH
    encoding = console.stderr_console.encoding or ""
    try:
        _PROGRESS_COMPLETE_GLYPH.encode(encoding)
        _PROGRESS_REMAINING_GLYPH.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return _PROGRESS_ASCII_COMPLETE_GLYPH, _PROGRESS_ASCII_REMAINING_GLYPH
    return _PROGRESS_COMPLETE_GLYPH, _PROGRESS_REMAINING_GLYPH


def progress_percent(*, completed: int, total: int) -> int:
    """Return a bounded integer percentage for progress display."""
    pct = 0 if total <= 0 else math.floor(completed / total * 100)
    pct = max(0, min(100, pct))
    if total > 0 and completed >= total:
        return 100
    return pct


def progress_bar_completed(*, completed: int, total: int, width: int) -> int:
    """Return the number of filled cells for a progress bar."""
    filled = 0 if total <= 0 else math.floor(width * completed / total)
    filled = max(0, min(width, filled))
    if total > 0 and 0 < completed < total:
        return max(1, min(width - 1, filled))
    if total > 0 and completed >= total:
        return width
    return filled


def render_progress_bar(
    *,
    completed: int,
    total: int,
    width: int = PROGRESS_BAR_WIDTH,
) -> Text:
    """Render a styled progress bar using terminal-safe glyphs."""
    filled = progress_bar_completed(
        completed=completed,
        total=total,
        width=width,
    )
    pct = progress_percent(completed=completed, total=total)
    complete_glyph, remaining_glyph = _progress_bar_glyphs()
    text = Text()
    text.append(
        complete_glyph * filled,
        style="bar.finished" if pct >= 100 else "bar.complete",
    )
    text.append(remaining_glyph * (width - filled), style="bar.back")
    return text


class StatusProgressRenderer:
    """Render structured progress updates from tailed logs."""

    def __init__(self, active_status: log.StatusHandle | None = None) -> None:
        self._progress: Progress | None = None
        self._task_id: TaskID | None = None
        self._active_status = active_status
        self._owns_live = False
        self._active = False
        self._closed = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        """Whether a progress renderable is active."""
        return self._active or self._progress is not None

    def update(self, update: ProgressUpdate) -> None:
        """Apply one structured progress update."""
        with self._lock:
            if self._closed:
                return
            if log.plain_output_enabled():
                return
            if update.clear_label:
                active_status = self._active_status or log.get_active_status()
                sticky_prefix = log.get_sticky_prefix()
                if active_status is not None and sticky_prefix is not None:
                    active_status.update(sticky_prefix)
                self._reset_owned()
                return
            description, subject = self._description(update)
            detail = self._detail(update.description)
            if self._active_status is not None:
                self._active_status.update(
                    _format_status_progress(
                        prefix=description,
                        detail=detail,
                        completed=update.completed,
                        total=update.total,
                    ),
                    sticky_prefix=description,
                    sticky_separator=" ",
                    sticky_subject=subject,
                )
                self._progress = None
                self._active = True
                return
            if self._progress is None:
                # TextColumn text is a str.format template: literal braces
                # in the description must be doubled or they raise/expand.
                column_text = description.replace("{", "{{").replace("}", "}}")
                self._progress = Progress(
                    SpinnerColumn(),
                    TextColumn(column_text),
                    BarColumn(bar_width=_PROGRESS_BAR_WIDTH),
                    _PaddedPercentColumn(),
                    TextColumn(
                        "{task.description}",
                        style="dim",
                        markup=False,
                    ),
                    transient=True,
                    console=console.stderr_console,
                )
                self._task_id = self._progress.add_task(
                    self._truncate_detail(description, detail),
                    total=update.total,
                )
                self._active_status = (
                    self._active_status or log.get_active_status()
                )
                if self._active_status is not None:
                    self._active_status.use_progress(self._progress)
                else:
                    self._progress.start()
                    self._owns_live = True
            if self._task_id is not None:
                self._progress.update(
                    self._task_id,
                    description=self._truncate_detail(description, detail),
                    completed=update.completed,
                    total=update.total,
                )

    def finish(self) -> None:
        """Stop progress rendering and resume any parent status."""
        with self._lock:
            self._closed = True
            if self._active_status is not None and self.active:
                self._active_status.resume_status()
            self._reset_owned()
            self._active_status = None

    def _reset_owned(self) -> None:
        """Stop any owned progress display and reset renderer state.

        Must be called with ``self._lock`` held. Every renderer field
        that tracks the current progress display is reset here so the
        ``clear_label`` and ``finish()`` teardown paths cannot drift.
        """
        if self._owns_live and self._progress is not None:
            self._progress.stop()
        self._progress = None
        self._task_id = None
        self._owns_live = False
        self._active = False

    @staticmethod
    def _description(update: ProgressUpdate) -> tuple[str, str | None]:
        """Return the (trusted-markup description, subject) for an update.

        ``update`` fields come from child-process progress lines and are
        untrusted: they are escaped here (``status_label().markup``
        escapes internally) so the returned description is safe to treat
        as Rich markup downstream.
        """
        if update.process is not None or update.subject is not None:
            process = update.process or log.get_sticky_process()
            subject = update.subject or log.get_sticky_subject()
            if process is not None and subject is not None:
                return log.status_label(process, subject).markup, subject
            if process is not None:
                return _escape_markup(process), None
            if subject is not None:
                return log.status_label("progress", subject).markup, subject
        if update.label is not None:
            return _escape_markup(update.label), None
        prefix = log.get_sticky_prefix()
        if prefix is None:
            return "progress", None
        return prefix, log.get_sticky_subject()

    @staticmethod
    def _detail(description: str) -> str:
        return description

    @staticmethod
    def _truncate_detail(description: str, detail: str) -> str:
        max_width = _progress_detail_width(description)
        text = Text(detail)
        if text.cell_len <= max_width:
            return detail
        text.truncate(max_width, overflow="ellipsis")
        return text.plain
