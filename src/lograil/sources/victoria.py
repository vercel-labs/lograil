# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""VictoriaLogs integration for lograil."""

from __future__ import annotations

from typing import Protocol

import contextlib
import json
import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from lograil._internal import remap
from lograil._internal.remap import RemapPipeline
from lograil._internal.tail import LogEntry, LogQuery, LogSource

__all__ = [
    "VICTORIA_LOGS_BASE",
    "VictoriaLogsSource",
    "VictoriaLogsStreamError",
    "build_logsql_query",
    "is_victoria_logs_available",
    "query_logs",
    "query_recent_entries",
]

VICTORIA_LOGS_BASE = "http://127.0.0.1:9428"
_logger = logging.getLogger("lograil.sources.victoria")
_SEVERITY_LEVELS = ("TRACE", "DEBUG", "INFO", "WARN", "ERROR", "CRITICAL")


class _ClosableIterator(Iterator[LogEntry], Protocol):
    def close(self) -> None: ...


class VictoriaLogsStreamError(Exception):
    """Fatal, non-retryable error from the VictoriaLogs tail endpoint.

    Deliberately not a RuntimeError subclass so that generic retry layers
    treating RuntimeError as transient do not swallow it.
    """


_FRACTIONAL_SECONDS = re.compile(r"\.(\d+)")


def _parse_timestamp(value: str) -> tuple[datetime, int] | None:
    """Parse an ISO-8601 timestamp, tolerating a trailing ``Z``.

    Returns ``(moment, nanoseconds)`` where ``moment`` carries no
    fractional seconds and ``nanoseconds`` holds the full sub-second
    precision.  VictoriaLogs emits nanosecond timestamps, which
    ``datetime.fromisoformat`` would silently truncate to microseconds --
    entries within the same microsecond must still compare correctly at
    reconnect seams.  Returns None when the value cannot be parsed.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    match = _FRACTIONAL_SECONDS.search(text)
    if match:
        nanos = int(match.group(1)[:9].ljust(9, "0"))
        text = text[: match.start()] + text[match.end() :]
    else:
        nanos = 0
    try:
        return datetime.fromisoformat(text), nanos
    except ValueError:
        return None


def _compare_timestamps(left: str, right: str) -> int | None:
    """Compare two raw timestamps at full nanosecond precision.

    Returns a negative/zero/positive int like a comparator, or None when
    either side cannot be parsed or the pair is not comparable (naive vs
    aware) -- callers must treat None as "do not drop".
    """
    lhs = _parse_timestamp(left)
    rhs = _parse_timestamp(right)
    if lhs is None or rhs is None:
        return None
    try:
        if lhs < rhs:
            return -1
        if lhs > rhs:
            return 1
    except TypeError:
        return None
    return 0


def _advance_cursor(current: str | None, candidate: str) -> str:
    """Return the newer of two raw timestamp cursors.

    The tail stream can deliver entries out of order; a late older entry
    must not regress the resume cursor, or already-delivered newer entries
    would be replayed as duplicates after the next reconnect.
    """
    if current is None:
        return candidate
    order = _compare_timestamps(candidate, current)
    if order is None:
        # Not comparable: prefer the parseable side, else the newest seen.
        return candidate if _parse_timestamp(current) is None else current
    return candidate if order > 0 else current


def _response_excerpt(response: httpx.Response, limit: int = 200) -> str:
    """Return a short single-line excerpt of a response body."""
    try:
        text = response.text
    except (httpx.HTTPError, httpx.StreamError):
        # Body unavailable (e.g. unread or already-consumed stream).
        return ""
    return " ".join(text.split())[:limit]


def _victoria_entry(entry: LogEntry) -> LogEntry:
    """Translate VictoriaLogs fields to lograil's generic entry shape."""
    if "levelname" not in entry and "severity" in entry:
        entry["levelname"] = str(entry["severity"]).upper()
    if "name" not in entry and "service" in entry:
        entry["name"] = str(entry["service"])
    if "message" not in entry:
        if "_msg" in entry:
            entry["message"] = str(entry["_msg"])
        elif "msg" in entry:
            entry["message"] = str(entry["msg"])
    if "timestamp" not in entry and "_time" in entry:
        entry["timestamp"] = str(entry["_time"])
    return entry


VICTORIA_REMAPS = (_victoria_entry, *remap.DEFAULT_REMAPS)
_VICTORIA_PIPELINE = RemapPipeline(VICTORIA_REMAPS)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _normalize_severity(value: str) -> str:
    aliases = {"WARNING": "WARN", "FATAL": "CRITICAL"}
    normalized = aliases.get(value.upper(), value.upper())
    if normalized not in _SEVERITY_LEVELS:
        expected = ", ".join(_SEVERITY_LEVELS)
        msg = f"unknown severity {value!r}; expected one of {expected}"
        raise ValueError(msg)
    return normalized


def _normalize_entry(entry: dict[str, object]) -> LogEntry:
    """Apply VictoriaLogs remaps to one raw entry."""
    mapped = _VICTORIA_PIPELINE(entry)
    if mapped is None:
        return {}
    return mapped


def is_victoria_logs_available(base_url: str = VICTORIA_LOGS_BASE) -> bool:
    """Return whether VictoriaLogs is responding."""
    try:
        resp = httpx.get(f"{base_url}/health", timeout=2)
        return int(resp.status_code) == 200
    except (httpx.TransportError, OSError):
        return False


def build_logsql_query(
    *,
    services: list[str] | None = None,
    severity: str | None = None,
    vm_name: str | list[str] | None = None,
    source_type: str | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    fields: dict[str, str] | None = None,
) -> str:
    """Build a LogsQL query string."""
    parts: list[str] = []
    if deployment_id:
        parts.append(f"deployment_id:={_quote(deployment_id)}")
    if request_id:
        parts.append(f"request_id:={_quote(request_id)}")
    if fields:
        parts.extend(
            f"{name}:={_quote(fields[name])}" for name in sorted(fields)
        )
    if vm_name:
        if isinstance(vm_name, list):
            quoted = ", ".join(_quote(name) for name in vm_name)
            parts.append(f"vm_name:in({quoted})")
        else:
            parts.append(f"vm_name:={_quote(vm_name)}")
    if services:
        service_filters: list[str] = []
        for svc in services:
            if "*" not in svc:
                service_filters.append(f"service:={_quote(svc)}")
            elif svc.endswith("*") and "*" not in svc[:-1]:
                service_filters.append(f"service:{_quote(svc[:-1])}*")
            else:
                regex = re.escape(svc).replace(r"\*", ".*")
                service_filters.append(f"service:~{_quote(f'^{regex}$')}")
        parts.append(
            service_filters[0]
            if len(service_filters) == 1
            else "(" + " OR ".join(service_filters) + ")"
        )
    if source_type:
        if source_type.startswith("!"):
            parts.append(f"source_type:!={_quote(source_type[1:])}")
        else:
            parts.append(f"source_type:={_quote(source_type)}")
    if severity:
        severity_upper = _normalize_severity(severity)
        idx = _SEVERITY_LEVELS.index(severity_upper)
        values = ",".join(_quote(s) for s in _SEVERITY_LEVELS[idx:])
        parts.append(f"severity:in({values})")
    else:
        parts.append('severity:not("TRACE")')
    if not parts:
        return "*"
    return " AND ".join(parts)


def query_logs(
    query: str,
    *,
    limit: int = 1000,
    since: str | None = None,
    until: str | None = None,
    base_url: str = VICTORIA_LOGS_BASE,
) -> list[LogEntry]:
    """Query VictoriaLogs historical entries, oldest first."""
    url = f"{base_url}/select/logsql/query"
    try:
        entries: list[LogEntry] = []
        data: dict[str, str | int] = {"query": query, "limit": limit}
        if since:
            data["start"] = since
        if until:
            data["end"] = until
        with httpx.stream("POST", url, data=data, timeout=30) as resp:
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                entry = _entry_from_line(raw_line)
                if entry is not None:
                    entries.append(entry)
        entries.reverse()
        return entries
    except (httpx.HTTPError, OSError):
        _logger.exception("Failed to query VictoriaLogs")
        return []


def query_recent_entries(
    service: str | None = None,
    *,
    limit: int = 5,
    vm_name: str | list[str] | None = None,
    source_type: str | None = None,
    base_url: str = VICTORIA_LOGS_BASE,
) -> list[LogEntry]:
    """Query recent entries if VictoriaLogs is available."""
    if not is_victoria_logs_available(base_url):
        return []
    query = build_logsql_query(
        services=[service] if service is not None else None,
        vm_name=vm_name,
        source_type=source_type,
    )
    return query_logs(query, limit=limit, base_url=base_url)


@dataclass
class VictoriaLogsSource(LogSource, source_id="victoria"):
    """LogSource implementation backed by VictoriaLogs tail API."""

    base_url: str = VICTORIA_LOGS_BASE
    offset: str = "100ms"
    refresh_interval: str = "100ms"
    timeout: httpx.Timeout = field(
        default_factory=lambda: httpx.Timeout(5.0, read=1.0)
    )

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        """Open the tail API and yield an iterable entry handle."""
        # One client for the handle's lifetime: reconnects reuse pooled
        # connections instead of paying TCP setup on every read timeout.
        with httpx.Client(timeout=self.timeout) as client:
            entries = self._read_entries(stop=stop, query=query, client=client)
            try:
                yield entries
            finally:
                entries.close()

    def _read_entries(
        self,
        *,
        stop: threading.Event,
        client: httpx.Client,
        query: LogQuery | None = None,
    ) -> _ClosableIterator:
        """Yield tail API entries until stopped.

        Transient failures (transport errors, HTTP 5xx and 429, clean
        server-side stream ends) reconnect after a backoff, resuming from
        the last delivered timestamp. Other HTTP 4xx responses are fatal
        and raise :class:`VictoriaLogsStreamError`.

        Reconnect seams are deduplicated by dropping replayed entries at
        or before the resume cursor -- only during the catch-up phase
        right after a reconnect, never on a live uninterrupted stream.
        Trade-off: distinct entries sharing the cursor's exact timestamp
        that were not yet delivered before the reconnect are dropped at
        the seam; strictly newer entries are never dropped.
        """
        query_string = (
            str(query.get("query", "*")) if query is not None else "*"
        )
        url = f"{self.base_url}/select/logsql/tail"
        resume_from: str | None = None
        reconnecting = False
        while not stop.is_set():
            # Catch-up cursor: set only when reconnecting with a known
            # position (never on the fresh first connection). Cleared as
            # soon as a strictly newer entry proves we are past the seam.
            cursor = resume_from if reconnecting else None
            reconnecting = True
            try:
                for entry in self._stream_entries(
                    url,
                    query_string=query_string,
                    resume_from=resume_from,
                    stop=stop,
                    client=client,
                ):
                    timestamp = entry.get("timestamp")
                    ts_text = (
                        timestamp
                        if isinstance(timestamp, str) and timestamp
                        else None
                    )
                    if cursor is not None and ts_text is not None:
                        order = _compare_timestamps(ts_text, cursor)
                        if order is not None and order <= 0:
                            # Replayed entry at or before the cursor.
                            continue
                        if order is not None:
                            # Strictly newer: past the seam, stop
                            # filtering for the rest of this stream.
                            cursor = None
                    if ts_text is not None:
                        resume_from = _advance_cursor(resume_from, ts_text)
                    yield entry
            except (httpx.ReadTimeout, httpx.TransportError, OSError):
                if stop.wait(1.0):
                    return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status >= 500 or status == 429:
                    # Server-side or rate-limit trouble: back off and
                    # reconnect, like a transport error.
                    if stop.wait(1.0):
                        return
                else:
                    excerpt = _response_excerpt(exc.response)
                    detail = f": {excerpt}" if excerpt else ""
                    msg = (
                        "VictoriaLogs tail request to "
                        f"{url!r} failed with HTTP {status}{detail}"
                    )
                    raise VictoriaLogsStreamError(msg) from exc
            else:
                # Clean stream end: the server closed without error;
                # back off before reconnecting to avoid a hot loop.
                if stop.wait(1.0):
                    return

    def _stream_entries(
        self,
        url: str,
        *,
        query_string: str,
        resume_from: str | None,
        stop: threading.Event,
        client: httpx.Client,
    ) -> Iterator[LogEntry]:
        data = {
            "query": query_string,
            "offset": self.offset,
            "refresh_interval": self.refresh_interval,
        }
        if resume_from is not None:
            data["start"] = resume_from
        with client.stream(
            "POST",
            url,
            params={
                "_msg_field": "message",
                "_time_field": "timestamp",
            },
            data=data,
        ) as resp:
            if resp.status_code >= 400:
                # Buffer the error body so the HTTPStatusError raised
                # below carries a readable response text for callers.
                with contextlib.suppress(httpx.HTTPError, httpx.StreamError):
                    resp.read()
            resp.raise_for_status()
            for raw_line in resp.iter_lines():
                if stop.is_set():
                    break
                entry = _entry_from_line(raw_line)
                if entry is not None:
                    yield entry


def _entry_from_line(raw_line: str) -> LogEntry | None:
    line = raw_line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return _normalize_entry(parsed)
