# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Generic log tailing and status rendering."""

from __future__ import annotations

from typing import Any, ClassVar, TextIO

import contextlib
import contextvars
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from rich.markup import escape as _escape_markup

from lograil._internal import console, log, progress, remap
from lograil._internal.formatter import (
    format_log_entry,
    format_log_entry_renderable,
    format_spinner_entry,
)
from lograil._internal.registry import SourceRegistryBase
from lograil._internal.remap import Remap

LogEntry = dict[str, Any]
LogQuery = dict[str, Any]

_LOSSY_UPDATE_INTERVAL = 0.02


@dataclass
class _TailRenderContext:
    remap_pipeline: remap.RemapPipeline
    progress_renderer: progress.StatusProgressRenderer
    active_status: log.StatusHandle | None
    lossy: bool
    persistent: bool
    show_context: bool
    hide_context: str | None


class TailDrained(threading.Event):
    """Event set when the tail thread finishes.

    ``error`` holds the exception that terminated the source stream, or
    ``None`` when the stream drained cleanly.  Sources own their own
    reconnection/retry behavior; any exception that escapes ``open()`` or
    its entry iterator is treated as a stream failure and reported here
    rather than retried.
    """

    def __init__(self) -> None:
        super().__init__()
        self.error: BaseException | None = None


class LogSource(SourceRegistryBase):
    """Base class for backends that read structured log entries.

    ``open()`` returns a context manager whose handle yields entries until
    the stream is exhausted or ``stop`` is set.  The context manager owns any
    source resources; an exception escaping the handle iteration is final and
    is reported as a stream failure by the tailer.
    """

    _registry: ClassVar[dict[str, type[LogSource]]] = {}
    _registry_label: ClassVar[str] = "log source"

    @classmethod
    def from_stdin(cls, stdin: TextIO) -> LogSource:
        """Create a source that reads from ``stdin``."""
        _ = stdin
        msg = f"{cls.__name__} does not support stdin"
        raise NotImplementedError(msg)

    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> contextlib.AbstractContextManager[Iterable[LogEntry]]:
        """Open the source and return an iterable entry handle."""
        _ = stop, query
        msg = f"{type(self).__name__} does not implement open"
        raise NotImplementedError(msg)


def _progress_update_from_entry(
    entry: LogEntry,
) -> progress.ProgressUpdate | None:
    return progress.ProgressUpdate.from_mapping({
        "description": entry.get(remap.PROGRESS_DESCRIPTION),
        "completed": entry.get(remap.PROGRESS_COMPLETED),
        "total": entry.get(remap.PROGRESS_TOTAL),
        "label": entry.get(remap.PROGRESS_LABEL),
        "process": entry.get(remap.PROGRESS_PROCESS),
        "subject": entry.get(remap.PROGRESS_SUBJECT),
        "clear_label": entry.get(remap.PROGRESS_CLEAR_LABEL, False),
    })


def _render_tail_entry(
    raw_entry: LogEntry,
    *,
    context: _TailRenderContext,
    last_update: float,
) -> float:
    entry = context.remap_pipeline(raw_entry)
    if entry is None:
        return last_update
    progress_update = _progress_update_from_entry(entry)
    msg = entry.get("message")
    has_message = not (msg is None or (isinstance(msg, str) and not msg))
    # Only a missing/empty message means "nothing to say": falsy non-string
    # values such as 0 or False are legitimate content.
    if not has_message:
        if progress_update is None:
            return last_update
        msg_str = progress_update.description
    else:
        msg_str = str(msg)
    now = time.monotonic()
    levelno = log.level_number(str(entry.get("levelname", "INFO")))
    if (
        progress_update is not None
        and log.fancy_output_enabled()
        # Progress display is UI state, not log output: the bar renders
        # even when the env filter suppresses the entry's own text (only
        # an "off" directive silences it entirely).
        and log.entry_display_enabled(log.tail_logger().name)
    ):
        context.progress_renderer.update(progress_update)
        if levelno < logging.WARNING or not has_message:
            return last_update
    if not log.entry_enabled(log.tail_logger().name, levelno):
        return last_update
    if entry.get(remap.STATUS_ONLY) is True and log.fancy_output_enabled():
        if context.progress_renderer.active:
            return last_update
        if context.active_status is not None:
            # Source text is untrusted; StatusHandle.update treats plain
            # str as trusted Rich markup.
            context.active_status.update(_escape_markup(msg_str))
        # Status-only text is transient by definition: with no spinner to
        # show it on, it is dropped, never printed permanently.
        return last_update
    emitted = _emit_tail_entry(
        entry,
        msg_str,
        levelno=levelno,
        context=context,
        last_update=last_update,
        now=now,
    )
    return now if emitted else last_update


def _print_tail_entry(entry: LogEntry, *, show_context: bool) -> None:
    if log.plain_output_enabled() and not _message_has_ansi(entry):
        console.stderr_console.print(
            format_log_entry(
                entry,
                context="name" if show_context else None,
                target_console=console.stderr_console,
            ),
            soft_wrap=True,
        )
        return
    console.stderr_console.print(
        format_log_entry_renderable(
            entry,
            context="name" if show_context else None,
            target_console=console.stderr_console,
        ),
        soft_wrap=True,
    )


def _message_has_ansi(entry: LogEntry) -> bool:
    message = entry.get("message")
    return isinstance(message, str) and "\x1b[" in message


def emit_entry(
    entry: LogEntry,
    *,
    show_context: bool = True,
    logger_name: str | None = None,
) -> bool:
    """Emit one remapped entry through the tailer's output rules."""
    message = entry.get("message")
    if message is None or (isinstance(message, str) and not message):
        return False
    msg_str = str(message)
    levelno = log.level_number(str(entry.get("levelname", "INFO")))
    if not log.entry_enabled(logger_name or log.tail_logger().name, levelno):
        return False
    if log.output_mode() == "json":
        log.tail_logger().log(levelno, msg_str, extra={"lograil.entry": entry})
        return True
    _print_tail_entry(entry, show_context=show_context)
    return True


def _emit_tail_entry(
    entry: LogEntry,
    msg_str: str,
    *,
    levelno: int,
    context: _TailRenderContext,
    last_update: float,
    now: float,
) -> bool:
    entry_show_context = (
        context.show_context and entry.get("name") != context.hide_context
    )
    if context.persistent or log.plain_output_enabled():
        return emit_entry(entry, show_context=entry_show_context)
    if log.output_mode() == "json":
        return emit_entry(entry, show_context=entry_show_context)
    # Fancy mode: warnings and errors always print permanently (above any
    # active spinner or progress bar), never as droppable transient text.
    if levelno >= logging.WARNING:
        _print_tail_entry(entry, show_context=entry_show_context)
        return True
    if context.progress_renderer.active:
        # A progress bar owns the display; transient spinner lines are
        # suppressed.
        return False
    # The lossy throttle only rate-limits transient spinner updates;
    # permanently printed output is never dropped.
    if context.lossy and now - last_update < _LOSSY_UPDATE_INTERVAL:
        return False
    rendered = format_spinner_entry(
        entry,
        msg_str,
        show_context=entry_show_context,
    )
    if context.active_status is not None:
        context.active_status.update(
            rendered,
            sticky_prefix=log.get_sticky_prefix(),
        )
    else:
        log.tail_logger().log(levelno, rendered)
    return True


@contextlib.contextmanager
def tail_to_status(
    *,
    source: LogSource | None = None,
    filters: LogQuery | None = None,
    delay: float = 0.1,
    lossy: bool = True,
    persistent: bool = False,
    show_context: bool = False,
    hide_context: str | None = None,
    remaps: Iterable[Remap] | None = None,
) -> Iterator[TailDrained]:
    """Tail log entries into the active Rich status.

    Yields a :class:`TailDrained` event that is set when the source stream
    ends.  If the source failed, the exception is available on its ``error``
    attribute.  The stream is consumed exactly once: sources are responsible
    for their own reconnection, and a failure is never silently retried or
    replayed by the tailer.

    Entries render according to the active output mode: transient spinner
    updates in fancy mode (rate-limited when ``lossy`` is true), permanent
    timestamped lines in plain mode or when ``persistent`` is true, and
    NDJSON records in json mode.  Warnings and errors always print
    permanently, and the configured env filter applies in every mode.
    ``remaps`` overrides the default entry pipeline, and ``filters`` is
    passed through to the source's ``open()``.  ``delay`` postpones
    opening the source (letting an enclosing status settle first); the
    source is opened and consumed even when the ``with`` block finishes
    within the delay.
    """
    if source is None:
        drained = TailDrained()
        drained.set()
        yield drained
        return
    stop = threading.Event()
    active_status = log.get_active_status()
    progress_renderer = progress.StatusProgressRenderer(active_status)
    remap_pipeline = remap.RemapPipeline(
        remap.DEFAULT_REMAPS if remaps is None else remaps
    )
    drained = TailDrained()
    # The tail thread renders on behalf of this context: capture it so
    # context-local state (active status, sticky prefix) is visible there.
    creating_context = contextvars.copy_context()

    def _tail() -> None:
        # Startup delay only; stopping during it must not skip the source:
        # the stream is still opened and consumed (until ``stop``), keeping
        # the consumed-exactly-once contract for quick ``with`` blocks.
        stop.wait(delay)
        last_update = 0.0
        render_context = _TailRenderContext(
            remap_pipeline=remap_pipeline,
            progress_renderer=progress_renderer,
            active_status=active_status,
            lossy=lossy,
            persistent=persistent,
            show_context=show_context,
            hide_context=hide_context,
        )
        try:
            with source.open(stop=stop, query=filters) as entries:
                for raw_entry in entries:
                    if stop.is_set():
                        break
                    last_update = _render_tail_entry(
                        raw_entry,
                        context=render_context,
                        last_update=last_update,
                    )
        except Exception as exc:
            drained.error = exc
            log.tail_logger().warning(
                "log source %s failed: %s", type(source).__name__, exc
            )
            log.tail_logger().debug("log source failure", exc_info=exc)
        finally:
            drained.set()

    thread = threading.Thread(
        target=lambda: creating_context.run(_tail),
        name="lograil-tail",
        daemon=True,
    )
    thread.start()
    try:
        yield drained
    finally:
        stop.set()
        thread.join(timeout=2.0)
        progress_renderer.finish()


def run_source_to_status(
    source: LogSource,
    *,
    filters: LogQuery | None = None,
    remaps: Iterable[Remap] | None = None,
) -> int:
    """Consume ``source`` and render it through the status tailer.

    Returns 0 when the stream drained cleanly and 1 when the source failed.
    """
    status_context = (
        log.status("lograil", done=None)
        if log.fancy_output_enabled()
        else contextlib.nullcontext()
    )
    with (
        status_context,
        tail_to_status(
            source=source,
            filters=filters,
            delay=0,
            lossy=False,
            remaps=remaps,
        ) as drained,
    ):
        drained.wait()
    if drained.error is not None:
        return 1
    return 0


def stream_log_files(log_files: list[Path], *, tail: int | None = None) -> None:
    """Stream logs from multiple log files through the standard tailer.

    ``tail`` emits the last ``tail`` existing lines of each file before
    following new output.  Requires the ``file`` extra (watchdog).
    """
    try:
        from lograil.sources.file import (  # ruff:ignore[import-outside-top-level]
            FileLogSource,
        )
    except ModuleNotFoundError as exc:
        msg = (
            "stream_log_files requires the file-tailing extra; "
            "install with: pip install 'lograil[file]'"
        )
        raise RuntimeError(msg) from exc
    if not log_files:
        log.tail_logger().warning("No log files configured")
        return
    source = FileLogSource(log_files, read_from="end", tail_lines=tail)
    with contextlib.suppress(KeyboardInterrupt):
        run_source_to_status(source)
