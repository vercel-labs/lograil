# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Structured progress lines emitted by child processes or log streams."""

from __future__ import annotations

from typing import Any, TextIO, cast

import contextlib
import json
import math
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import IntEnum

from rich.markup import escape as _escape_markup
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TaskID,
    TextColumn,
)
from rich.table import Column
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
_TERMINAL_PROGRESS_PREFIX = "\x1b]9;4"
_TERMINAL_PROGRESS_SUFFIX = "\x1b\\"


class TerminalProgressState(IntEnum):
    """OSC 9;4 progress states understood by supporting terminals."""

    HIDDEN = 0
    NORMAL = 1
    ERROR = 2
    INDETERMINATE = 3
    PAUSED = 4


class TerminalProgressHandle:
    """Emit terminal-native progress without reserving an inline row."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = sys.stderr if stream is None else stream
        try:
            is_terminal = self._stream.isatty()
        except OSError:
            is_terminal = False
        self._enabled = log.fancy_output_enabled() and is_terminal
        self._active = False
        self._lock = threading.Lock()

    @property
    def active(self) -> bool:
        """Whether this handle currently owns terminal progress state."""
        return self._active

    def update(
        self,
        percent: int | None = None,
        *,
        state: TerminalProgressState = TerminalProgressState.NORMAL,
    ) -> None:
        """Set terminal progress, using ``None`` for indeterminate work."""
        if state is TerminalProgressState.HIDDEN:
            self.clear()
            return
        if percent is None:
            if state not in {
                TerminalProgressState.NORMAL,
                TerminalProgressState.INDETERMINATE,
            }:
                raise ValueError(
                    "terminal progress without a percentage must be "
                    "indeterminate"
                )
            state = TerminalProgressState.INDETERMINATE
            percent = 0
        elif state is TerminalProgressState.INDETERMINATE:
            raise ValueError(
                "indeterminate terminal progress cannot have a percentage"
            )
        if isinstance(percent, bool) or not 0 <= percent <= 100:
            raise ValueError("terminal progress percentage must be 0..100")
        with self._lock:
            self._write(state, percent)
            self._active = self._enabled

    def clear(self) -> None:
        """Remove terminal progress state if this handle displayed it."""
        with self._lock:
            if not self._active:
                return
            self._write(TerminalProgressState.HIDDEN, 0)
            self._active = False

    def _write(self, state: TerminalProgressState, percent: int) -> None:
        if not self._enabled:
            return
        self._stream.write(
            f"{_TERMINAL_PROGRESS_PREFIX};{state.value};{percent}"
            f"{_TERMINAL_PROGRESS_SUFFIX}"
        )
        self._stream.flush()


@contextlib.contextmanager
def terminal_progress(
    *, stream: TextIO | None = None
) -> Iterator[TerminalProgressHandle]:
    """Yield an OSC 9;4 terminal-progress handle and clear it on exit."""
    handle = TerminalProgressHandle(stream)
    try:
        yield handle
    finally:
        handle.clear()


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
    separator: str = " "
    clear_label: bool = False
    child_description: str | None = None
    child_completed: int = 0
    child_total: int | None = None

    @classmethod
    def from_mapping(  # noqa: PLR0911, PLR0912
        cls, data: Mapping[str, Any]
    ) -> ProgressUpdate | None:
        """Validate generic progress metadata from a mapping."""
        description = data.get("description")
        completed = _coerce_integer(data.get("completed"))
        raw_total = data.get("total")
        total = _coerce_integer(raw_total)
        label = data.get("label")
        process = data.get("process")
        subject = data.get("subject")
        separator = data.get("separator", " ")
        clear_label = data.get("clear_label", False)
        child_description = data.get("child_description")
        raw_child_completed = data.get("child_completed")
        child_completed = _coerce_integer(raw_child_completed)
        raw_child_total = data.get("child_total")
        child_total = _coerce_integer(raw_child_total)
        if not isinstance(description, str):
            return None
        if completed is None:
            return None
        if raw_total is not None and total is None:
            return None
        if label is not None and not isinstance(label, str):
            return None
        if process is not None and not isinstance(process, str):
            return None
        if subject is not None and not isinstance(subject, str):
            return None
        if not isinstance(separator, str):
            return None
        if not isinstance(clear_label, bool):
            return None
        if child_description is not None and not isinstance(
            child_description, str
        ):
            return None
        if child_description is None:
            if raw_child_completed is not None or raw_child_total is not None:
                return None
            child_completed = 0
        elif child_completed is None:
            return None
        if raw_child_total is not None and child_total is None:
            return None
        return cls(
            description=description,
            completed=completed,
            total=total,
            label=label,
            process=process,
            subject=subject,
            separator=separator,
            clear_label=clear_label,
            child_description=child_description,
            child_completed=child_completed,
            child_total=child_total,
        )


def _coerce_integer(value: object) -> int | None:
    """Accept native integers and canonical decimal strings from log stores."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        if value == str(parsed):
            return parsed
    return None


def lograil_instrumentation_env() -> Mapping[str, str]:
    """Return env vars that enable lograil child instrumentation."""
    return {_PROGRESS_LINES_ENV: "1"}


def should_emit_progress_lines() -> bool:
    """Return whether child code should emit parent-readable progress."""
    return os.environ.get(_PROGRESS_LINES_ENV) == "1"


def format_line(  # noqa: PLR0913
    *,
    description: str,
    completed: int,
    total: int | None = None,
    label: str | None = None,
    process: str | None = None,
    subject: str | None = None,
    separator: str = " ",
    clear_label: bool = False,
    child_description: str | None = None,
    child_completed: int = 0,
    child_total: int | None = None,
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
    if separator != " ":
        payload["separator"] = separator
    if clear_label:
        payload["clear_label"] = True
    if child_description is not None:
        payload["child_description"] = child_description
        payload["child_completed"] = child_completed
        if child_total is not None:
            payload["child_total"] = child_total
    return prefix + json.dumps(payload, sort_keys=True)


def emit_line(line: str) -> None:
    """Write one preformatted structured progress update to stdout."""
    print(line, flush=True)  # ruff:ignore[print] - subprocess IPC via stdout.


def emit(  # noqa: PLR0913
    *,
    description: str,
    completed: int,
    total: int | None = None,
    label: str | None = None,
    process: str | None = None,
    subject: str | None = None,
    separator: str = " ",
    clear_label: bool = False,
    child_description: str | None = None,
    child_completed: int = 0,
    child_total: int | None = None,
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
            separator=separator,
            clear_label=clear_label,
            child_description=child_description,
            child_completed=child_completed,
            child_total=child_total,
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
            return Text(" " * _PROGRESS_PERCENT_WIDTH)
        percentage = math.floor(task.percentage)
        if task.total and task.completed >= task.total:
            percentage = 100
        return Text(f"{percentage:>3.0f}%", style="progress.percentage")


class _OptionalBarColumn(BarColumn):
    """Keep rows aligned while omitting indeterminate bars."""

    def render(self, task: Any) -> Any:
        if task.total is None:
            return Text(" " * _PROGRESS_BAR_WIDTH)
        return super().render(task)


class _HierarchySpinnerColumn(SpinnerColumn):
    """Render a branch marker instead of a second spinner for child work."""

    def render(self, task: Any) -> Any:
        if task.fields.get("is_child"):
            return Text(" └─", style="dim")
        return super().render(task)


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
        self._child_task_id: TaskID | None = None
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
                if active_status is not None:
                    active_status.restore()
                self._reset_owned()
                return
            description, subject = self._description(update)
            detail = self._detail(update.description)
            if (
                self._active_status is not None
                and update.child_description is None
                and self._progress is None
            ):
                self._active_status.update(
                    _format_status_progress(
                        prefix=description,
                        detail=detail,
                        completed=update.completed,
                        total=update.total,
                    ),
                    sticky_prefix=description,
                    sticky_separator=update.separator,
                    sticky_subject=subject,
                )
                self._progress = None
                self._active = True
                return
            if self._progress is None:
                self._progress = Progress(
                    _HierarchySpinnerColumn(),
                    TextColumn("{task.fields[label]}"),
                    _OptionalBarColumn(bar_width=_PROGRESS_BAR_WIDTH),
                    _PaddedPercentColumn(),
                    TextColumn(
                        "{task.description}",
                        style="dim",
                        markup=False,
                        table_column=Column(
                            ratio=1,
                            no_wrap=True,
                            overflow="ellipsis",
                        ),
                    ),
                    transient=True,
                    console=console.stderr_console,
                    expand=True,
                )
                self._task_id = self._progress.add_task(
                    self._truncate_detail(description, detail),
                    total=update.total,
                    label=description,
                    is_child=False,
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
                    label=description,
                )
            child_complete = (
                update.child_total is not None
                and update.child_total > 0
                and update.child_completed >= update.child_total
            )
            if update.child_description is None or child_complete:
                if self._child_task_id is not None:
                    self._progress.remove_task(self._child_task_id)
                    self._child_task_id = None
                return
            child_label = _escape_markup(update.child_description)
            if self._child_task_id is None:
                self._child_task_id = self._progress.add_task(
                    "",
                    total=update.child_total,
                    label=child_label,
                    is_child=True,
                )
            self._progress.update(
                self._child_task_id,
                completed=update.child_completed,
                total=update.child_total,
                label=child_label,
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
        self._child_task_id = None
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
