# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing_extensions import Self

import threading
from collections.abc import Iterable

import httpx
import pytest

from lograil.sources.victoria import (
    VictoriaLogsSource,
    VictoriaLogsStreamError,
    _normalize_entry,
    build_logsql_query,
)


def _collect_source(
    source: VictoriaLogsSource,
    *,
    stop: threading.Event | None = None,
) -> list[dict[str, object]]:
    stop = threading.Event() if stop is None else stop
    with source.open(stop=stop) as entries:
        return list(entries)


def _status_error(status_code: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://victoria/tail")
    response = httpx.Response(status_code, request=request, text=body)
    return httpx.HTTPStatusError(
        "bad status", request=request, response=response
    )


class FakeResponse:
    def __init__(
        self,
        lines: Iterable[str],
        *,
        status_code: int = 200,
        body: str = "",
    ) -> None:
        self._lines = list(lines)
        self.status_code = status_code
        self._body = body

    def iter_lines(self) -> Iterable[str]:
        yield from self._lines

    def read(self) -> bytes:
        return self._body.encode()

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise _status_error(self.status_code, self._body)


class FakeStream:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.exited = False

    def __enter__(self) -> FakeResponse:
        return self._response

    def __exit__(self, *args: object) -> None:
        _ = args
        self.exited = True


class FakeClient:
    def __init__(self, stream: FakeStream) -> None:
        self._stream = stream

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        _ = args

    def stream(self, *args: object, **kwargs: object) -> FakeStream:
        _ = args, kwargs
        return self._stream


def test_victoria_source_exposes_open_method() -> None:
    source = VictoriaLogsSource()

    assert callable(source.open)


def test_victoria_source_default_read_timeout_is_bounded() -> None:
    source = VictoriaLogsSource()

    assert source.timeout.read == pytest.approx(1.0)


def test_build_logsql_query_supports_filters() -> None:
    result = build_logsql_query(
        services=["functions/*/51-litellm*"],
        severity="ERROR",
        deployment_id="dpl_abc123",
        fields={"run_id": "run-123"},
    )

    assert 'deployment_id:="dpl_abc123"' in result
    assert 'run_id:="run-123"' in result
    assert 'service:~"^functions/.*/51\\\\-litellm.*$"' in result
    assert 'severity:in("ERROR","CRITICAL")' in result


def test_build_logsql_query_escapes_filter_values() -> None:
    result = build_logsql_query(
        services=['api" OR *'],
        deployment_id='dpl"x\\y',
    )

    assert 'deployment_id:="dpl\\"x\\\\y"' in result
    assert 'service:"api\\" OR "*' in result


def test_build_logsql_query_accepts_severity_aliases() -> None:
    assert build_logsql_query(severity="WARNING") == (
        'severity:in("WARN","ERROR","CRITICAL")'
    )


def test_build_logsql_query_defaults_to_excluding_trace() -> None:
    assert build_logsql_query() == 'severity:not("TRACE")'


def test_victoria_entry_normalization_maps_raw_fields() -> None:
    result = _normalize_entry({
        "_msg": "ready",
        "_time": "2024-01-01T00:00:00Z",
        "service": "api",
        "severity": "warn",
    })

    assert result["message"] == "ready"
    assert result["timestamp"] == "2024-01-01T00:00:00Z"
    assert result["name"] == "api"
    assert result["levelname"] == "WARN"


def test_victoria_stream_raises_for_http_errors() -> None:
    client = FakeClient(FakeStream(FakeResponse([], status_code=400)))
    source = VictoriaLogsSource(base_url="http://victoria")

    with pytest.raises(httpx.HTTPStatusError):
        list(
            source._stream_entries(
                "http://victoria/select/logsql/tail",
                query_string="*",
                resume_from=None,
                stop=threading.Event(),
                client=client,  # type: ignore[arg-type]
            )
        )


def test_victoria_read_backs_off_after_clean_stream_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    waits: list[float] = []

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        stop.set()
        return True

    monkeypatch.setattr(stop, "wait", wait)
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(
        source,
        "_stream_entries",
        lambda *args, **kwargs: iter(()),
    )

    assert _collect_source(source, stop=stop) == []
    assert waits == [1.0]


def test_victoria_read_timeout_checks_stop_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    waits: list[float] = []

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        stop.set()
        return True

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        _ = args, kwargs
        raise httpx.ReadTimeout("idle stream")
        yield {}  # pragma: no cover - makes this a generator

    monkeypatch.setattr(stop, "wait", wait)
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    assert _collect_source(source, stop=stop) == []
    assert waits == [1.0]


def test_victoria_source_closes_stream_when_context_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = FakeStream(
        FakeResponse([
            '{"timestamp":"2024-01-01T00:00:00Z","message":"first"}',
            '{"timestamp":"2024-01-01T00:00:01Z","message":"second"}',
        ])
    )
    monkeypatch.setattr(
        httpx, "Client", lambda *args, **kwargs: FakeClient(stream)
    )
    stop = threading.Event()
    source = VictoriaLogsSource(base_url="http://victoria")

    with source.open(stop=stop) as entries:
        assert next(entries)["message"] == "first"

    assert stream.exited is True


def test_victoria_read_filters_resume_boundary_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T00:00:00Z", "message": "first"}
            return
        yield {"timestamp": "2024-01-01T00:00:00Z", "message": "first"}
        yield {"timestamp": "2024-01-01T00:00:01Z", "message": "second"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["first", "second"]


def test_victoria_read_cursor_does_not_regress_on_out_of_order_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T00:00:05Z", "message": "newer"}
            # Delivered out of order; must not regress the resume cursor.
            yield {"timestamp": "2024-01-01T00:00:03Z", "message": "late"}
            return
        yield {"timestamp": "2024-01-01T00:00:05Z", "message": "newer"}
        yield {"timestamp": "2024-01-01T00:00:06Z", "message": "fresh"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == [
        "newer",
        "late",
        "fresh",
    ]


def test_victoria_read_keeps_same_microsecond_newer_entry_at_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {
                "timestamp": "2024-01-01T00:00:00.123456700Z",
                "message": "first",
            }
            return
        yield {
            "timestamp": "2024-01-01T00:00:00.123456700Z",
            "message": "first",
        }
        # Strictly newer by nanoseconds only: must survive the seam even
        # though both timestamps land in the same microsecond.
        yield {
            "timestamp": "2024-01-01T00:00:00.123456900Z",
            "message": "second",
        }
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["first", "second"]


@pytest.mark.parametrize("status_code", [500, 503, 429])
def test_victoria_read_retries_retryable_statuses_with_backoff(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    stop = threading.Event()
    waits: list[float] = []
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            raise _status_error(status_code, "temporarily unavailable")
        yield {"timestamp": "2024-01-01T00:00:00Z", "message": "after"}
        stop.set()

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["after"]
    assert calls == 2
    assert waits[0] == pytest.approx(1.0)


@pytest.mark.parametrize("status_code", [400, 401, 403, 404])
def test_victoria_read_raises_descriptive_error_for_fatal_statuses(
    monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    stop = threading.Event()

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        _ = args, kwargs
        raise _status_error(status_code, "cannot parse start param")
        yield {}  # pragma: no cover - makes this a generator

    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    with pytest.raises(VictoriaLogsStreamError) as exc_info:
        _collect_source(source, stop=stop)

    message = str(exc_info.value)
    assert f"HTTP {status_code}" in message
    assert "cannot parse start param" in message
    assert isinstance(exc_info.value.__cause__, httpx.HTTPStatusError)
    # Must not be swallowed by generic RuntimeError retry layers.
    assert not isinstance(exc_info.value, RuntimeError)


def test_victoria_read_reconnects_after_clean_end_with_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    waits: list[float] = []
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T00:00:00Z", "message": "one"}
            return  # server closed cleanly
        yield {"timestamp": "2024-01-01T00:00:01Z", "message": "two"}
        stop.set()

    def wait(timeout: float) -> bool:
        waits.append(timeout)
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["one", "two"]
    assert calls == 2
    # The clean end must be followed by a backoff wait, not a hot loop.
    assert waits[0] == pytest.approx(1.0)


def test_victoria_read_delivers_same_timestamp_siblings_mid_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        _ = args, kwargs
        yield {"timestamp": "2024-01-01T00:00:00Z", "message": "a"}
        yield {"timestamp": "2024-01-01T00:00:00Z", "message": "b"}
        yield {"timestamp": "2024-01-01T00:00:00Z", "message": "c"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["a", "b", "c"]


def test_victoria_read_catch_up_filter_stops_after_first_newer_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T00:00:02Z", "message": "one"}
            return
        # Replay after reconnect: older and equal entries are dropped,
        # the first strictly newer entry disables the filter, and a
        # subsequent out-of-order older entry is still delivered.
        yield {"timestamp": "2024-01-01T00:00:01Z", "message": "older"}
        yield {"timestamp": "2024-01-01T00:00:02Z", "message": "dup"}
        yield {"timestamp": "2024-01-01T00:00:03Z", "message": "newer"}
        yield {"timestamp": "2024-01-01T00:00:02Z", "message": "late"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["one", "newer", "late"]


def test_victoria_read_compares_timestamps_across_precisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {
                "timestamp": "2024-01-01T10:00:00Z",
                "message": "whole",
            }
            return
        # Lexicographically '...00.500Z' <= '...00Z' ('.' < 'Z'), but the
        # parsed datetime is strictly newer and must not be dropped.
        yield {
            "timestamp": "2024-01-01T10:00:00.500Z",
            "message": "subsecond",
        }
        # A '+00:00' rendering equal to the cursor's 'Z' rendering would
        # compare wrongly as a string; here it is strictly newer.
        yield {
            "timestamp": "2024-01-01T10:00:01+00:00",
            "message": "offset",
        }
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == [
        "whole",
        "subsecond",
        "offset",
    ]


def test_victoria_read_drops_equal_offset_rendering_at_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T10:00:00Z", "message": "one"}
            return
        # Same instant rendered with '+00:00' instead of 'Z' -- string
        # comparison would treat it as newer; parsed it is a duplicate.
        yield {
            "timestamp": "2024-01-01T10:00:00+00:00",
            "message": "dup",
        }
        yield {"timestamp": "2024-01-01T10:00:01Z", "message": "two"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == ["one", "two"]


def test_victoria_read_keeps_unparseable_timestamps_during_catch_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = threading.Event()
    calls = 0

    def stream(*args: object, **kwargs: object) -> Iterable[dict[str, str]]:
        nonlocal calls
        _ = args, kwargs
        calls += 1
        if calls == 1:
            yield {"timestamp": "2024-01-01T00:00:01Z", "message": "one"}
            return
        yield {"timestamp": "not-a-timestamp", "message": "odd"}
        yield {"message": "bare"}
        yield {"timestamp": "2024-01-01T00:00:01Z", "message": "dup"}
        yield {"timestamp": "2024-01-01T00:00:02Z", "message": "two"}
        stop.set()

    monkeypatch.setattr(stop, "wait", lambda timeout: stop.is_set())
    source = VictoriaLogsSource(base_url="http://victoria")
    monkeypatch.setattr(source, "_stream_entries", stream)

    entries = _collect_source(source, stop=stop)

    assert [entry["message"] for entry in entries] == [
        "one",
        "odd",
        "bare",
        "two",
    ]
