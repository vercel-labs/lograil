# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral log entry formatting."""

from __future__ import annotations

from typing import Any, Literal

import colorsys
import hashlib
import json
import logging
import traceback
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from logging import LogRecord

from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape as _escape_markup
from rich.text import Text

from lograil._internal import console

OutputMode = Literal["plain", "json", "fancy"]

_MIN_CONTRAST = 8.0
_SRGB_LINEAR_THRESHOLD = 0.04045
_BASIC_COLORS = ["cyan", "green", "magenta", "yellow", "red"]
_256_STEPS = [0, 0x5F, 0x87, 0xAF, 0xD7, 0xFF]
_STANDARD_LOG_RECORD_ATTRS = frozenset({
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "thread",
    "threadName",
})


def _short_log_value(value: object) -> str:
    text = str(value).strip()
    if text.startswith(("/", "./", "~/")) and " " not in text:
        # A filesystem path: the basename is the informative part.  Other
        # slash-bearing values (URLs, ratios, MIME types) must stay whole.
        text = text.rsplit("/", 1)[-1]
    if len(text) > 120:
        text = text[:119] + "..."
    return text


def detail_parts(entry: Mapping[str, Any]) -> list[str]:
    """Return concise ``key=value`` details for extra structured fields."""
    core_fields = {
        "asctime",
        "created",
        "levelname",
        "level",
        "message",
        "msg",
        "name",
        "timestamp",
    }
    parts: list[str] = []
    for field in sorted(entry):
        if field in core_fields:
            continue
        value = entry.get(field)
        if value is None or (isinstance(value, str) and not value):
            continue
        formatted = _short_log_value(value)
        if formatted:
            parts.append(f"{field}={formatted}")
    return parts


def _relative_luminance(r: float, g: float, b: float) -> float:
    def _lin(c: float) -> float:
        if c <= _SRGB_LINEAR_THRESHOLD:
            return c / 12.92
        return float(((c + 0.055) / 1.055) ** 2.4)

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _dark_contrast(r: float, g: float, b: float) -> float:
    return (_relative_luminance(r, g, b) + 0.05) / 0.05


def _256_to_rgb(idx: int) -> tuple[float, float, float]:
    idx -= 16
    return (
        _256_STEPS[idx // 36] / 255,
        _256_STEPS[(idx % 36) // 6] / 255,
        _256_STEPS[idx % 6] / 255,
    )


_EXTENDED_COLORS = [
    i
    for i in range(16, 232)
    if _dark_contrast(*_256_to_rgb(i)) >= _MIN_CONTRAST
]


def _stable_hash(name: str) -> int:
    return int.from_bytes(
        hashlib.md5(name.encode(), usedforsecurity=False).digest()[:4],
        byteorder="big",
    )


def context_style(name: str, target_console: Console | None = None) -> str:
    """Return a dim, hash-based color style for a context name."""
    target = (
        console.stdout_console if target_console is None else target_console
    )
    return _context_style(name, target.color_system)


@lru_cache(maxsize=256)
def _context_style(name: str, color_system: str | None) -> str:
    """Return a dim, hash-based color style for a console color system."""
    h = _stable_hash(name)
    if color_system == "truecolor":
        hue = h % 360
        r, g, b = 1.0, 1.0, 1.0
        for lightness in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
            r, g, b = colorsys.hls_to_rgb(hue / 360, lightness, 0.7)
            if _dark_contrast(r, g, b) >= _MIN_CONTRAST:
                break
        color = f"rgb({int(r * 255)},{int(g * 255)},{int(b * 255)})"
    elif color_system == "256":
        color = f"color({_EXTENDED_COLORS[h % len(_EXTENDED_COLORS)]})"
    else:
        color = _BASIC_COLORS[h % len(_BASIC_COLORS)]
    return f"dim {color}"


SEVERITY_TIMESTAMP_STYLES: dict[str, str] = {
    "TRACE": "dim",
    "DEBUG": "dim cyan",
    "INFO": "blue",
    "WARN": "yellow",
    "WARNING": "yellow",
    "ERROR": "red",
    "FATAL": "bold red",
    "CRITICAL": "bold red",
}

SPINNER_SEVERITY_STYLES: dict[str, str] = {
    "TRACE": "dim",
    "DEBUG": "dim cyan",
    "INFO": "dim",
    "WARN": "dim yellow",
    "WARNING": "dim yellow",
    "ERROR": "dim red",
    "FATAL": "dim bold red",
    "CRITICAL": "dim bold red",
}

_PLAIN_SEVERITY_STYLES: dict[int, str] = {
    logging.DEBUG: "dim cyan",
    logging.INFO: "blue",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "bold red",
}


def _plain_style_for_level(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return _PLAIN_SEVERITY_STYLES[logging.CRITICAL]
    if levelno >= logging.ERROR:
        return _PLAIN_SEVERITY_STYLES[logging.ERROR]
    if levelno >= logging.WARNING:
        return _PLAIN_SEVERITY_STYLES[logging.WARNING]
    if levelno >= logging.INFO:
        return _PLAIN_SEVERITY_STYLES[logging.INFO]
    return _PLAIN_SEVERITY_STYLES[logging.DEBUG]


def _format_record_time(dt: datetime) -> str:
    frac = dt.microsecond // 100000
    return f"{dt.strftime('%H:%M:%S')}.{frac}"


class LograilFormatter(logging.Formatter):
    """Format stdlib log records for lograil output modes."""

    def __init__(
        self,
        *,
        output_mode: OutputMode = "plain",
        show_tracebacks: bool = False,
    ) -> None:
        super().__init__()
        self.output_mode = output_mode
        self.show_tracebacks = show_tracebacks

    def format(self, record: LogRecord) -> str:
        """Format ``record`` for the configured lograil output mode."""
        if self.output_mode == "json":
            return self._format_json(record)
        return self._format_plain(record)

    def format_renderable(self, record: LogRecord) -> Text | str:
        """Format ``record`` as a Rich renderable when useful."""
        if self.output_mode == "plain":
            return self._format_plain_text(record)
        return self.format(record)

    def _format_json(self, record: LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        entry = record.__dict__.get("lograil.entry")
        if isinstance(entry, Mapping):
            # The entry's own fields win over the record-derived defaults:
            # a tailed historical entry keeps its original timestamp and
            # level rather than the emission time and the level coerced
            # for stdlib logging.
            for key, value in entry.items():
                if key == "message":
                    continue
                if key == "levelname":
                    payload["level"] = str(value)
                    continue
                payload[key] = value
        if record.exc_info and self.show_tracebacks:
            payload["exception"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
        return json.dumps(payload, sort_keys=True, default=str)

    def _format_plain(self, record: LogRecord) -> str:
        style = _plain_style_for_level(record.levelno)
        dt = datetime.fromtimestamp(record.created).astimezone()
        ts = _format_record_time(dt)
        message = _escape_markup(record.getMessage())
        indent = " " * (len(ts) + 1)
        message_lines = message.splitlines() or [""]
        rendered = f"[{style}]{ts}[/{style}] {message_lines[0]}"
        for line in message_lines[1:]:
            rendered += f"\n{indent}{line}"
        if record.exc_info and self.show_tracebacks:
            exc_text = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
            if exc_text:
                for line in exc_text.splitlines():
                    rendered += f"\n{indent}[dim]{_escape_markup(line)}[/dim]"
        return rendered

    def _format_plain_text(self, record: LogRecord) -> Text:
        style = _plain_style_for_level(record.levelno)
        dt = datetime.fromtimestamp(record.created).astimezone()
        ts = _format_record_time(dt)
        indent = " " * (len(ts) + 1)
        message_lines = record.getMessage().splitlines() or [""]

        rendered = Text(ts, style=style)
        rendered.append(" ")
        rendered.append_text(Text.from_ansi(message_lines[0]))
        for line in message_lines[1:]:
            rendered.append("\n")
            rendered.append(indent)
            rendered.append_text(Text.from_ansi(line))
        if record.exc_info and self.show_tracebacks:
            exc_text = "".join(
                traceback.format_exception(*record.exc_info)
            ).rstrip()
            if exc_text:
                for line in exc_text.splitlines():
                    rendered.append("\n")
                    rendered.append(indent)
                    rendered.append(line, style="dim")
        return rendered


def format_spinner_entry(
    entry: Mapping[str, Any],
    msg: str,
    *,
    show_context: bool,
) -> str:
    """Format a log entry for display in a transient status spinner."""
    levelname = str(entry.get("levelname", "INFO")).upper()
    style = SPINNER_SEVERITY_STYLES.get(levelname, "dim")
    escaped = _escape_markup(msg)
    result: str
    if show_context and (name := str(entry.get("name", ""))):
        svc_style = context_style(name)
        escaped_name = _escape_markup(name)
        result = (
            f"[{svc_style}]{escaped_name}[/{svc_style}] "
            f"[{style}]{escaped}[/{style}]"
        )
    else:
        result = f"[{style}]{escaped}[/{style}]"
    return result


def _normalize_entry(entry: LogRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(entry, LogRecord):
        normalized: dict[str, Any] = {
            "created": entry.created,
            "levelname": entry.levelname,
            "message": entry.getMessage(),
            "name": entry.name,
        }
        normalized.update({
            key: value
            for key, value in entry.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_ATTRS
            and value is not None
            and not (isinstance(value, str) and not value)
        })
        return normalized

    return dict(entry)


def _format_time(entry: Mapping[str, Any]) -> str:
    if asctime := entry.get("asctime"):
        return str(asctime)
    raw = entry.get("timestamp")
    if raw is None:
        raw = entry.get("created")
    if raw is None or (isinstance(raw, str) and not raw):
        return ""
    if isinstance(raw, int | float):
        try:
            dt = datetime.fromtimestamp(raw).astimezone()
        except (OverflowError, OSError, ValueError):
            return str(raw)
        return _format_record_time(dt)
    text = str(raw)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return _format_record_time(dt.astimezone())
    except (ValueError, OSError):
        return text


def format_log_entry(
    entry: LogRecord | Mapping[str, Any],
    *,
    oneline: bool = False,
    context: str | None = "name",
    include_extra: bool = False,
    width: int | None = None,
    target_console: Console | None = None,
) -> str:
    """Format a LogRecord-like entry for terminal display.

    ``width`` overrides the terminal width; ``None`` queries
    ``target_console`` per call, defaulting to the stdout console.
    """
    render_console = (
        console.stdout_console if target_console is None else target_console
    )
    normalized = _normalize_entry(entry)
    message = str(normalized.get("message", ""))
    parts = detail_parts(normalized) if include_extra else []
    if parts:
        message = f"{message} ({', '.join(parts)})"
    levelname = str(normalized.get("levelname", "INFO")).upper()

    ctx_name = normalized.get(context) if context is not None else None
    ctx_name = str(ctx_name) if ctx_name else None

    time_str = _format_time(normalized)
    sev_style = SEVERITY_TIMESTAMP_STYLES.get(levelname, "")
    ts_str = f"[{sev_style}]{time_str}[/{sev_style}]" if sev_style else time_str

    if ctx_name:
        style = context_style(ctx_name, render_console)
        ctx_prefix = f"[{style}]{_escape_markup(ctx_name)}[/{style}] "
        ctx_visible_len = cell_len(ctx_name) + 1
    else:
        ctx_prefix = ""
        ctx_visible_len = 0

    term_width = width if width is not None else render_console.width
    if oneline:
        separator_width = 1 if time_str else 0
        max_msg = (
            term_width - cell_len(time_str) - separator_width - ctx_visible_len
        )
        if max_msg > 4 and cell_len(message) > max_msg:
            text = Text(message)
            text.truncate(max_msg - 2, overflow="crop", pad=False)
            message = text.plain + "->"
        message = _escape_markup(message)
    else:
        # Wrap the RAW message, then escape each wrapped line: running
        # width logic on the escaped string both mis-measures the visible
        # length and lets a wrap split an escape sequence between "\" and
        # "[", producing live markup mid-message.
        full_prefix_len = len(time_str) + 1 + ctx_visible_len
        first_line_avail = term_width - full_prefix_len
        if first_line_avail > 20 and cell_len(message) > first_line_avail:
            message = _escape_wrapped_message(
                message,
                width=term_width,
                first_prefix_len=full_prefix_len,
                subsequent_prefix_len=len(time_str) + 1,
            )
        else:
            message = _escape_markup(message)

    if time_str:
        return f"{ts_str} {ctx_prefix}{message}"
    return f"{ctx_prefix}{message}"


def format_log_entry_renderable(
    entry: LogRecord | Mapping[str, Any],
    *,
    context: str | None = "name",
    include_extra: bool = False,
    target_console: Console | None = None,
) -> Text:
    """Format a log entry as Rich text, decoding ANSI in the message only."""
    render_console = (
        console.stdout_console if target_console is None else target_console
    )
    normalized = _normalize_entry(entry)
    message = str(normalized.get("message", ""))
    parts = detail_parts(normalized) if include_extra else []
    if parts:
        message = f"{message} ({', '.join(parts)})"
    levelname = str(normalized.get("levelname", "INFO")).upper()

    ctx_name = normalized.get(context) if context is not None else None
    ctx_name = str(ctx_name) if ctx_name else None

    time_str = _format_time(normalized)
    sev_style = SEVERITY_TIMESTAMP_STYLES.get(levelname, "")
    result = Text()
    if time_str:
        result.append(time_str, style=sev_style)
        result.append(" ")

    if ctx_name:
        result.append(ctx_name, style=context_style(ctx_name, render_console))
        result.append(" ")

    result.append_text(Text.from_ansi(message))
    return result


def _escape_wrapped_message(
    message: str,
    *,
    width: int,
    first_prefix_len: int,
    subsequent_prefix_len: int,
) -> str:
    """Wrap long physical lines without flattening embedded newlines.

    Widths are measured in terminal cells, not characters, so wide (CJK)
    text wraps at the same point the trigger in :func:`format_log_entry`
    measured instead of overflowing the terminal.
    """
    rendered: list[str] = []
    indent = " " * subsequent_prefix_len
    for index, physical_line in enumerate(message.split("\n")):
        prefix_len = first_prefix_len if index == 0 else 0
        if cell_len(physical_line) <= width - prefix_len:
            rendered.append(_escape_markup(physical_line))
            continue
        pieces = _wrap_line_cells(
            physical_line,
            first_width=width - prefix_len,
            rest_width=width - subsequent_prefix_len,
        )
        rendered.append(_escape_markup(pieces[0]))
        rendered.extend(indent + _escape_markup(piece) for piece in pieces[1:])
    return "\n".join(rendered)


def _wrap_line_cells(
    line: str, *, first_width: int, rest_width: int
) -> list[str]:
    """Greedily wrap ``line`` into pieces of bounded cell width.

    Breaks after the last whitespace that fits when there is one, else
    hard-breaks at the cell budget.
    """
    pieces: list[str] = []
    budget = max(first_width, 1)
    while cell_len(line) > budget:
        split = _cell_split_index(line, budget)
        for index in range(split, 0, -1):
            if line[index - 1].isspace():
                split = index
                break
        pieces.append(line[:split])
        line = line[split:]
        budget = max(rest_width, 1)
    pieces.append(line)
    return pieces


def _cell_split_index(text: str, budget: int) -> int:
    """Return the largest prefix length of ``text`` within ``budget`` cells."""
    total = 0
    for index, char in enumerate(text):
        total += cell_len(char)
        if total > budget:
            return max(index, 1)
    return len(text)
