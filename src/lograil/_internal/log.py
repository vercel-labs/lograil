# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Rich logging and status utilities."""

from __future__ import annotations

from typing import TYPE_CHECKING

import contextlib
import datetime
import logging
import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass

from rich.logging import RichHandler
from rich.markup import escape
from rich.text import Text

from lograil._internal.console import stderr_console
from lograil._internal.formatter import LograilFormatter, OutputMode

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rich.console import RenderableType
    from rich.status import Status

_DONE_UNSET = object()
LOG_ENV = "LOGRAIL"
OUTPUT_ENV = "LOGRAIL_OUTPUT"
TRACEBACK_ENV = "LOGRAIL_LOG_ERROR_TRACEBACK"
LOGGER_NAME = "lograil"


_LEVEL_NAMES: dict[str, int] = {
    "TRACE": logging.DEBUG,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
    "FATAL": logging.CRITICAL,
    "CRITICAL": logging.CRITICAL,
}


def _parse_level(value: str) -> int | None:
    return _LEVEL_NAMES.get(value.strip().upper())


def _valid_target(value: str) -> bool:
    if not value or any(ch in value for ch in "[]{}"):
        return False
    return all(part.isidentifier() for part in value.split("."))


def _target_matches(target: str, logger_name: str) -> bool:
    return logger_name == target or logger_name.startswith(f"{target}.")


@dataclass(frozen=True)
class _Directive:
    target: str | None
    level: int | None
    order: int

    @property
    def specificity(self) -> int:
        if self.target is None:
            return 0
        return len(self.target.split("."))


class EnvFilter(logging.Filter):
    """Filter stdlib log records using tracing-style env directives."""

    def __init__(
        self, spec: str | None = None, *, default: str = "info"
    ) -> None:
        super().__init__()
        default_level = _parse_level(default) or logging.INFO
        self.default_level = default_level
        self.directives = self._parse(spec or "")

    @classmethod
    def from_env(cls, envvar: str, *, default: str = "info") -> EnvFilter:
        """Create an env filter from ``envvar`` with a default fallback."""
        return cls(os.environ.get(envvar), default=default)

    @property
    def min_level(self) -> int:
        """Lowest enabled level in this filter.

        Only levels that are actually effective for some logger count:
        each targeted directive contributes its winning threshold, and
        the global fallback is the last untargeted directive (or the
        default when none exists).  With everything ``off`` no level is
        enabled and a level above CRITICAL is returned.
        """
        levels = [
            level
            for level in (
                self.level_for(directive.target)
                for directive in self.directives
                if directive.target is not None
            )
            if level is not None
        ]
        global_level = self._global_level()
        if global_level is not None:
            levels.append(global_level)
        if not levels:
            return logging.CRITICAL + 10
        return min(levels)

    def _global_level(self) -> int | None:
        """Effective level for loggers matching no targeted directive."""
        best: _Directive | None = None
        for directive in self.directives:
            if directive.target is None and (
                best is None or directive.order > best.order
            ):
                best = directive
        if best is None:
            return self.default_level
        return best.level

    def enabled_for(self, logger_name: str, levelno: int) -> bool:
        """Return whether ``logger_name`` is enabled for ``levelno``."""
        level = self.level_for(logger_name)
        return level is not None and levelno >= level

    def level_for(self, logger_name: str) -> int | None:
        """Return the effective threshold for ``logger_name``."""
        best: _Directive | None = None
        for directive in self.directives:
            if directive.target is not None and not _target_matches(
                directive.target, logger_name
            ):
                continue
            if best is None:
                best = directive
                continue
            if directive.specificity > best.specificity:
                best = directive
                continue
            if (
                directive.specificity == best.specificity
                and directive.order > best.order
            ):
                best = directive
        if best is None:
            return self.default_level
        return best.level

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter ``record`` according to its logger name and level."""
        return self.enabled_for(record.name, record.levelno)

    @staticmethod
    def _parse(spec: str) -> list[_Directive]:
        directives: list[_Directive] = []
        for raw in spec.split(","):
            item = raw.strip()
            if not item:
                continue
            order = len(directives)
            if "=" in item:
                target, level_name = (
                    part.strip() for part in item.split("=", 1)
                )
                if not _valid_target(target):
                    continue
                if level_name.lower() == "off":
                    directives.append(_Directive(target, None, order))
                    continue
                level = _parse_level(level_name)
                if level is None:
                    continue
                directives.append(_Directive(target, level, order))
                continue
            level = _parse_level(item)
            if level is not None:
                directives.append(_Directive(None, level, order))
                continue
            if item.lower() == "off":
                directives.append(_Directive(None, None, order))
                continue
            if _valid_target(item):
                directives.append(_Directive(item, logging.DEBUG, order))
        return directives


def _get_error_traceback_mode() -> str:
    mode = os.environ.get(TRACEBACK_ENV, "auto").lower()
    if mode in {"always", "never", "auto"}:
        return mode
    return "auto"


def _default_output_mode() -> OutputMode:
    return "fancy" if sys.stderr.isatty() else "plain"


def _get_output_mode() -> OutputMode:
    mode = os.environ.get(OUTPUT_ENV, "").strip().lower()
    if mode == "plain":
        return "plain"
    if mode == "json":
        return "json"
    if mode == "fancy":
        return "fancy"
    return _default_output_mode()


def should_show_error_traceback() -> bool:
    """Return whether rich error tracebacks should be rendered."""
    logger = globals().get("_logger")
    logger_name = (
        logger.name if isinstance(logger, logging.Logger) else LOGGER_NAME
    )
    return _should_show_error_traceback(_env_filter, logger_name)


def _should_show_error_traceback(
    env_filter: EnvFilter, logger_name: str = LOGGER_NAME
) -> bool:
    mode = _get_error_traceback_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return env_filter.enabled_for(logger_name, logging.DEBUG)


def _format_log_time(dt: datetime.datetime) -> Text:
    frac = dt.microsecond // 100000
    return Text(f"{dt.strftime('%H:%M:%S')}.{frac}")


class _PlainLogHandler(logging.Handler):
    """Render plain output logs in timestamp-first tail style."""

    def emit(self, record: logging.LogRecord) -> None:
        stderr_console.print(
            LograilFormatter(
                output_mode="plain",
                show_tracebacks=should_show_error_traceback(),
            ).format_renderable(record),
            soft_wrap=True,
        )


class _JsonLogHandler(logging.Handler):
    """Render log records as newline-delimited JSON."""

    def emit(self, record: logging.LogRecord) -> None:
        rendered = LograilFormatter(
            output_mode="json",
            show_tracebacks=should_show_error_traceback(),
        ).format(record)
        sys.stderr.write(f"{rendered}\n")


class LograilHandler(logging.Handler):
    """Stdlib logging handler for lograil output modes."""

    def __init__(self, *, output_mode: OutputMode | None = None) -> None:
        super().__init__()
        self.output_mode: OutputMode = output_mode or _get_output_mode()
        self._handler = self._make_handler(self.output_mode)

    @staticmethod
    def _make_handler(output_mode: OutputMode) -> logging.Handler:
        if output_mode == "json":
            return _JsonLogHandler()
        if output_mode == "plain":
            return _PlainLogHandler()
        return RichHandler(
            console=stderr_console,
            show_time=False,
            show_level=True,
            show_path=False,
            rich_tracebacks=should_show_error_traceback(),
            markup=True,
            log_time_format=_format_log_time,
        )

    def setLevel(self, level: int | str) -> None:  # noqa: N802
        """Set the threshold on this handler and its delegate."""
        super().setLevel(level)
        self._handler.setLevel(level)

    def emit(self, record: logging.LogRecord) -> None:
        """Emit ``record`` through the configured delegate handler."""
        self._handler.emit(record)


def _setup_logging(
    env_filter: EnvFilter,
    *,
    logger_name: str = LOGGER_NAME,
    output_mode: OutputMode,
) -> logging.Logger:
    """Set up Rich logging with EnvFilter control."""
    log_level = env_filter.min_level
    handler: logging.Handler = LograilHandler(output_mode=output_mode)
    handler.setLevel(log_level)
    handler.addFilter(env_filter)
    logger = logging.getLogger(logger_name)
    logger.setLevel(log_level)
    logger.filters.clear()
    logger.addFilter(env_filter)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
    return logger


_env_filter = EnvFilter.from_env(LOG_ENV)
_output_mode = _get_output_mode()
_logger = _setup_logging(_env_filter, output_mode=_output_mode)


def configure_logging(
    envvar: str = LOG_ENV, default: str = "info", logger_name: str = LOGGER_NAME
) -> logging.Logger:
    """Configure lograil logging from EnvFilter directives."""
    global _env_filter, _logger, _output_mode
    _env_filter = EnvFilter.from_env(envvar, default=default)
    _output_mode = _get_output_mode()
    _logger = _setup_logging(
        _env_filter, logger_name=logger_name, output_mode=_output_mode
    )
    if logger_name != LOGGER_NAME:
        # Loggers under the fixed "lograil" namespace (the sources) must
        # follow the new configuration too, not the import-time handler
        # left on the base logger.
        _setup_logging(
            _env_filter, logger_name=LOGGER_NAME, output_mode=_output_mode
        )
    return _logger


def tail_logger() -> logging.Logger:
    """Return the logger for tailed-entry output.

    A child of the configured logger, so tail output follows
    :func:`configure_logging` -- including a custom ``logger_name`` -- and
    reaches any spinner handler installed on the configured logger.
    """
    return logging.getLogger(f"{_logger.name}.tail")


def output_mode() -> OutputMode:
    """Return the active lograil output mode."""
    return _output_mode


def plain_output_enabled() -> bool:
    """Return whether plain output mode is active."""
    return _output_mode == "plain"


def fancy_output_enabled() -> bool:
    """Return whether fancy terminal output mode is active."""
    return _output_mode == "fancy"


def level_number(levelname: str) -> int:
    """Return the stdlib level number for ``levelname`` (INFO if unknown)."""
    return _parse_level(levelname) or logging.INFO


_quiet_level: int = logging.NOTSET


def entry_enabled(logger_name: str, levelno: int) -> bool:
    """Return whether the active env filter enables ``levelno`` output."""
    if levelno < _quiet_level:
        return False
    return _env_filter.enabled_for(logger_name, levelno)


def entry_display_enabled(logger_name: str) -> bool:
    """Return whether any output at all is enabled for ``logger_name``.

    Progress bars and similar UI state render regardless of the per-entry
    level threshold, but an ``off`` directive silences them too.
    """
    return _env_filter.level_for(logger_name) is not None


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Temporarily raise the lograil log level to WARNING.

    Suppresses INFO/DEBUG output for the duration of the ``with`` block,
    restoring the previous level on exit.  Useful around chatty sections
    whose routine output would drown out what matters.  Applies both to
    stdlib log records and to tailed entries, which are gated through
    :func:`entry_enabled` rather than logger levels.
    """
    global _quiet_level
    prev_quiet = _quiet_level
    _quiet_level = max(prev_quiet, logging.WARNING)
    prev_level = _logger.level
    for handler in _logger.handlers:
        handler.setLevel(logging.WARNING)
    _logger.setLevel(logging.WARNING)
    try:
        yield
    finally:
        _quiet_level = prev_quiet
        _logger.setLevel(prev_level)
        for handler in _logger.handlers:
            handler.setLevel(prev_level)


_SPINNER_OVERHEAD = 3


@dataclass(frozen=True)
class StatusLabel:
    """Structured status label rendered as ``<process> <subject>``."""

    process: str
    subject: str

    @property
    def markup(self) -> str:
        """Rich markup representation of the label."""
        return (
            f"{escape(self.process)} "
            f"[bold blue]{escape(self.subject)}[/bold blue]"
        )

    @property
    def plain(self) -> str:
        """Plain text representation of the label."""
        return f"{self.process} {self.subject}"


def status_label(process: str, subject: str) -> StatusLabel:
    """Return a structured ``<process> <subject>`` status label.

    The label renders with the subject highlighted in fancy mode and as
    plain ``process subject`` text otherwise; pass it anywhere
    :func:`status` or :meth:`StatusHandle.update` accepts a message.
    """
    return StatusLabel(process=process, subject=subject)


def _format_status_message(
    message: str | StatusLabel | None,
    *,
    process: str | None = None,
    subject: str | None = None,
) -> tuple[str, str | None, str | None]:
    if message is not None and (process is not None or subject is not None):
        raise ValueError("status accepts either message or process/subject")
    if isinstance(message, StatusLabel):
        rendered = message.markup if fancy_output_enabled() else message.plain
        return rendered, message.process, message.subject
    if process is not None or subject is not None:
        process = process or get_sticky_process()
        subject = subject or get_sticky_subject()
        if process is None:
            raise ValueError("process requires message or sticky process")
        if subject is None:
            # The process string is untrusted text like any other status
            # message; the rendered value must be safe Rich markup.
            rendered = escape(process) if fancy_output_enabled() else process
            return rendered, process, None
        label = StatusLabel(process=process, subject=subject)
        rendered = label.markup if fancy_output_enabled() else label.plain
        return rendered, label.process, label.subject
    if message is None:
        raise ValueError("status requires message or process/subject")
    return message, None, None


def _derive_done(
    done: str | object | None,
    *,
    message: str,
    process: str | None,
    subject: str | None,
) -> str | None:
    if done is not _DONE_UNSET:
        return done if isinstance(done, str) else None
    if process is not None and subject is not None:
        return f"{message}: done"
    return None


def _fit_spinner_text(message: str | Text) -> Text:
    """Fit spinner text to the console width.

    A plain ``str`` here is TRUSTED Rich markup: untrusted text must be
    escaped where it enters lograil (log record messages in
    ``_SpinnerHandler.emit``, user-supplied status text in ``status()`` /
    ``update_status()``), never at this choke point.
    """
    text = (
        message.copy()
        if isinstance(message, Text)
        else Text.from_markup(message)
    )
    max_width = stderr_console.width - _SPINNER_OVERHEAD
    if max_width <= 4:
        return text
    if text.cell_len > max_width:
        # Leave room for the two-cell "->" continuation marker.
        text.truncate(max_width - 2, pad=False)
        text.append("->")
    return text


def _with_sticky_prefix(
    message: str | Text,
    prefix: str | None,
    *,
    separator: str = ": ",
    sticky_subject: str | None = None,
) -> str | Text:
    if prefix is None:
        return message
    if sticky_subject is not None and sticky_subject == get_sticky_subject():
        prefix = ""
        separator = ""
    if isinstance(message, Text):
        text = Text.from_markup(prefix)
        text.append(separator)
        text.append_text(message)
        return text
    return f"{prefix}{separator}{message}"


def _maybe_prefix_sticky(
    message: str,
    done: str | None,
    *,
    subject: str | None,
) -> tuple[str, str | None]:
    prefix = get_sticky_prefix()
    if prefix is None or message == prefix:
        return message, done
    if subject is not None and subject == get_sticky_subject():
        return message, done
    message = f"{prefix}: {message}"
    if done is not None:
        done = f"{prefix}: {done}"
    return message, done


class _SpinnerHandler(logging.Handler):
    """Log handler that forwards messages to a Rich Status spinner."""

    def __init__(
        self,
        rich_status: Status,
        original_handlers: list[logging.Handler],
        prior_deferred: list[logging.LogRecord] | None = None,
    ) -> None:
        super().__init__()
        self._status = rich_status
        self._original_handlers = original_handlers
        self._deferred = prior_deferred if prior_deferred is not None else []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self._deferred.append(record)
            return
        # Record messages are untrusted text; the sticky prefix is trusted
        # markup maintained by lograil.
        msg = escape(record.getMessage())
        prefix = get_sticky_prefix()
        if prefix is not None:
            msg = f"{prefix}: {msg}"
        self._status.update(_fit_spinner_text(msg))

    def take_deferred(self) -> list[logging.LogRecord]:
        """Remove and return all deferred records."""
        records = self._deferred
        self._deferred = []
        return records

    def flush_deferred(self) -> None:
        """Print deferred warning/error records."""
        for record in self._deferred:
            for handler in self._original_handlers:
                handler.emit(record)
        self._deferred.clear()


class StatusHandle:
    """Handle for updating a running status spinner."""

    def __init__(
        self,
        *,
        _status: Status | None,
        done: str | None,
        _original_handlers: list[logging.Handler] | None = None,
        _parent: StatusHandle | None = None,
        _spinner_handler: _SpinnerHandler | None = None,
    ) -> None:
        """Initialize a status handle."""
        self._status = _status
        self.done = done
        self._original_handlers = _original_handlers
        self._handlers_restored = False
        self._parent = _parent
        self._spinner_handler = _spinner_handler
        self.done_style = "success"

    @property
    def is_nested(self) -> bool:
        """Whether this handle is nested under another spinner."""
        return self._parent is not None

    def _root_status(self) -> Status | None:
        cur: StatusHandle | None = self
        while cur is not None:
            if cur._status is not None:
                return cur._status
            cur = cur._parent
        return None

    def update(
        self,
        message: str | Text | StatusLabel | None = None,
        *,
        sticky_prefix: str | None = None,
        sticky_separator: str = ": ",
        sticky_subject: str | None = None,
        process: str | None = None,
        subject: str | None = None,
        update_sticky: bool = False,
    ) -> None:
        """Replace the spinner text while the status is running.

        ``message`` may be plain text, trusted Rich markup, a ``Text``
        instance, or a :class:`StatusLabel`; alternatively pass
        ``process``/``subject`` to compose a structured label.  In plain
        and json output modes the update is logged at INFO instead of
        animating a spinner.  ``sticky_prefix`` prepends the given prefix
        (see :func:`get_sticky_prefix`), and ``update_sticky=True`` makes
        this message the new sticky prefix for nested statuses.
        """
        message_process: str | None = None
        message_subject: str | None = None
        if isinstance(message, Text):
            if process is not None or subject is not None:
                raise ValueError(
                    "status update accepts either Text or process/subject"
                )
            if update_sticky:
                raise ValueError("Text status updates cannot update sticky")
        else:
            message, message_process, message_subject = _format_status_message(
                message,
                process=process,
                subject=subject,
            )
            sticky_subject = sticky_subject or message_subject
        message = _with_sticky_prefix(
            message,
            sticky_prefix,
            separator=sticky_separator,
            sticky_subject=sticky_subject,
        )
        status_obj = self._root_status()
        if status_obj is not None:
            status_obj.update(_fit_spinner_text(message))
        else:
            rendered = message.plain if isinstance(message, Text) else message
            _logger.info(rendered)
        if update_sticky:
            _sticky_state.set((
                message.plain if isinstance(message, Text) else message,
                message_process,
                message_subject,
            ))

    def cancel(
        self, *, active: str = "cancelling", done: str = "cancelled"
    ) -> None:
        """Render a cancellation state."""
        self.update(active, sticky_prefix=get_sticky_prefix())
        self.done = done
        self.done_style = "warning"

    def stop(self) -> None:
        """Stop the underlying Rich status, if present."""
        if self._status is not None:
            self._status.stop()

    def start(self) -> None:
        """Start the underlying Rich status, if present."""
        if self._status is not None:
            self._status.start()

    def use_progress(self, progress: RenderableType | None = None) -> None:
        """Switch from spinner to a Rich renderable without flicker."""
        if self._parent is not None:
            self._parent.use_progress(progress)
            return
        if self._status is not None:
            if progress is not None:
                self._status._live.update(progress, refresh=True)
            else:
                self._status.stop()
        if self._original_handlers is not None and not self._handlers_restored:
            _logger.handlers = list(self._original_handlers)
            self._handlers_restored = True

    def resume_status(self, message: str | None = None) -> None:
        """Switch back from a progress renderable to the spinner."""
        if self._parent is not None:
            self._parent.resume_status(message)
            return
        if self._status is not None and self._handlers_restored:
            if message is not None:
                self._status.update(_fit_spinner_text(message))
            self._status._live.update(self._status.renderable, refresh=True)
            self._status.start()
            original_handlers = self._original_handlers or []
            prior = (
                self._spinner_handler.take_deferred()
                if self._spinner_handler is not None
                else None
            )
            new_handler = _SpinnerHandler(
                self._status, original_handlers, prior
            )
            new_handler.setLevel(logging.DEBUG)
            _logger.handlers = [new_handler]
            self._spinner_handler = new_handler
            self._handlers_restored = False


_active_status: ContextVar[StatusHandle | None] = ContextVar(
    "_active_status", default=None
)
# (prefix, process, subject) of the sticky status.  A ContextVar, like
# _active_status, so concurrent async tasks each keep their own sticky
# state instead of clobbering a shared global; threads that render on
# behalf of another context (the tail thread) capture that context at
# creation and run inside it.
_sticky_state: ContextVar[tuple[str | None, str | None, str | None]] = (
    ContextVar("_sticky_state", default=(None, None, None))
)


def get_active_status() -> StatusHandle | None:
    """Return the innermost active StatusHandle, or None."""
    return _active_status.get(None)


def get_sticky_prefix() -> str | None:
    """Return the current sticky prefix, or None."""
    return _sticky_state.get()[0]


def get_sticky_process() -> str | None:
    """Return the current sticky process, or None."""
    return _sticky_state.get()[1]


def get_sticky_subject() -> str | None:
    """Return the current sticky subject, or None."""
    return _sticky_state.get()[2]


def update_status(*, subject: str, process: str | None = None) -> None:
    """Replace the active structured sticky status label, if any."""
    active = get_active_status()
    if active is None and process is None and get_sticky_process() is None:
        _logger.info(subject)
        return
    if active is not None and process is None and get_sticky_process() is None:
        rendered = escape(subject) if fancy_output_enabled() else subject
        active.update(rendered, update_sticky=True)
        return
    message, message_process, message_subject = _format_status_message(
        None,
        process=process,
        subject=subject,
    )
    if active is None:
        _logger.info(message)
        return
    active.update(
        process=message_process, subject=message_subject, update_sticky=True
    )


@contextlib.contextmanager
def pause_for_prompt() -> Iterator[None]:
    """Pause any active spinner while prompting interactively."""
    active = get_active_status()
    if active is None:
        yield
        return
    active.use_progress(None)
    try:
        yield
    finally:
        active.resume_status()


@contextlib.contextmanager
def status(
    message: str | StatusLabel | None = None,
    *,
    done: str | object | None = _DONE_UNSET,
    sticky: bool = False,
    process: str | None = None,
    subject: str | None = None,
) -> Iterator[StatusHandle]:
    """Render a task status for the duration of the ``with`` block.

    In fancy mode this shows a transient spinner (nested statuses reuse
    the root spinner); in plain and json modes the message is logged at
    INFO.  On successful exit a permanent completion line is rendered:
    ``done`` supplies its text, or it is derived as ``"<message>: done"``
    when ``process`` and ``subject`` are given.  Pass ``done=None`` to
    suppress it; it is also skipped when the block raises.

    ``process``/``subject`` compose a structured label instead of
    ``message``.  ``sticky=True`` makes this status's message the prefix
    for nested statuses and tailed log lines.  Yields a
    :class:`StatusHandle` for mid-flight updates and cancellation.
    """
    if fancy_output_enabled():
        # User-supplied text is untrusted: escape it here, at the entry
        # point, so everything downstream is trusted markup.
        if isinstance(message, str):
            message = escape(message)
        if isinstance(done, str):
            done = escape(done)
    message, message_process, message_subject = _format_status_message(
        message,
        process=process,
        subject=subject,
    )
    done = _derive_done(
        done,
        message=message,
        process=message_process,
        subject=message_subject,
    )
    message, done = _maybe_prefix_sticky(message, done, subject=message_subject)
    if sticky:
        sticky_token = _sticky_state.set((
            message,
            message_process,
            message_subject,
        ))
    else:
        # Anchor a restore point: update_status() inside this block may
        # overwrite the sticky state even for a non-sticky status.
        sticky_token = _sticky_state.set(_sticky_state.get())

    try:
        if output_mode() != "fancy":
            _logger.info(message)
            handle = StatusHandle(_status=None, done=done)
            token = _active_status.set(handle)
            exc_raised = True
            try:
                yield handle
                exc_raised = False
            finally:
                _active_status.reset(token)
                if handle.done_style == "warning":
                    exc_raised = False
                if not exc_raised and handle.done is not None:
                    if handle.done_style == "warning":
                        _logger.warning(handle.done)
                    else:
                        _logger.info(handle.done)
            return

        parent = _active_status.get(None)
        if parent is not None and (
            parent._status is not None or parent.is_nested
        ):
            root_status = parent._root_status()
            if root_status is not None:
                root_status.update(_fit_spinner_text(message))
            handle = StatusHandle(_status=None, done=done, _parent=parent)
            token = _active_status.set(handle)
            exc_raised = True
            try:
                yield handle
                exc_raised = False
            finally:
                _active_status.reset(token)
                if handle.done_style == "warning":
                    exc_raised = False
                if not exc_raised and handle.done is not None:
                    done_marker = (
                        "[yellow]![/yellow]"
                        if handle.done_style == "warning"
                        else "[green]+[/green]"
                    )
                    # Printing on the live console renders the done line
                    # above the still-running parent spinner.
                    stderr_console.print(f"{done_marker} {handle.done}")
            return

        with stderr_console.status(_fit_spinner_text(message)) as rich_status:
            original_handlers = _logger.handlers[:]
            spinner_handler = _SpinnerHandler(rich_status, original_handlers)
            spinner_handler.setLevel(logging.DEBUG)
            _logger.handlers = [spinner_handler]
            handle = StatusHandle(
                _status=rich_status,
                done=done,
                _original_handlers=original_handlers,
                _spinner_handler=spinner_handler,
            )
            token = _active_status.set(handle)
            exc_raised = True
            try:
                yield handle
                exc_raised = False
            finally:
                _active_status.reset(token)
                if not handle._handlers_restored:
                    _logger.handlers = original_handlers
                if handle._spinner_handler is not None:
                    handle._spinner_handler.flush_deferred()
                if handle.done_style == "warning":
                    exc_raised = False
                if not exc_raised and handle.done is not None:
                    done_marker = (
                        "[yellow]![/yellow]"
                        if handle.done_style == "warning"
                        else "[green]+[/green]"
                    )
                    if not handle._handlers_restored:
                        rich_status._live.transient = False
                        rich_status._live.update(f"{done_marker} {handle.done}")
                    else:
                        stderr_console.print(f"{done_marker} {handle.done}")
    finally:
        _sticky_state.reset(sticky_token)
