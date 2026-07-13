# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import _patch, patch

import pytest

from lograil import (
    LogEntry,
    LogQuery,
    LogSource,
    configure_logging,
    format_progress_line,
    tail_to_status,
)
from lograil._internal import console
from lograil._internal.tail import _print_tail_entry, run_source_to_status


def _patch_tail_log() -> _patch[object]:
    return patch.object(logging.getLogger("lograil.tail"), "log")


class Handle:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.resumed = False

    def update(self, message: object, **kwargs: object) -> None:
        _ = kwargs
        self.messages.append(message)

    def use_progress(self, progress: object) -> None:
        _ = progress

    def resume_status(self) -> None:
        self.resumed = True


class FakeSource(LogSource):
    def __init__(self, entries: list[LogEntry]) -> None:
        self.entries = entries

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        _ = query
        yield self._entries(stop=stop)

    def _entries(self, *, stop: threading.Event) -> Iterator[LogEntry]:
        for entry in self.entries:
            if stop.is_set():
                break
            yield entry


def test_tail_to_status_uses_generic_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {"message": "Container starting"},
        {"message": "Ready"},
    ])

    with (
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    messages = [call.args[1] for call in mock_log.call_args_list]
    assert any("Container starting" in message for message in messages)
    assert any("Ready" in message for message in messages)


def test_tail_to_status_routes_progress_lines_to_active_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    line = format_progress_line(
        description="Uploading artifacts", completed=1, total=2
    )
    source = FakeSource([{"message": line}])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        patch(
            "lograil._internal.log.plain_output_enabled",
            return_value=False,
        ),
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    assert handle.messages
    assert any(
        "Uploading artifacts" in str(message) for message in handle.messages
    )
    assert mock_log.call_count == 0


def test_tail_to_status_applies_remaps_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([{"message": "raw"}])

    def remap_entry(entry: LogEntry) -> LogEntry:
        entry["message"] = "mapped"
        return entry

    with (
        _patch_tail_log() as mock_log,
        tail_to_status(
            source=source,
            delay=0,
            lossy=False,
            remaps=[remap_entry],
        ),
    ):
        threading.Event().wait(0.05)

    messages = [call.args[1] for call in mock_log.call_args_list]
    assert any("mapped" in message for message in messages)
    assert not any("raw" in message for message in messages)


def test_tail_to_status_uses_preexisting_progress_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "lograil.progress.description": "Packing layer",
            "lograil.progress.completed": 3,
            "lograil.progress.total": 4,
        }
    ])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        patch(
            "lograil._internal.log.plain_output_enabled",
            return_value=False,
        ),
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    assert any("Packing layer" in str(message) for message in handle.messages)
    assert mock_log.call_count == 0


def test_tail_to_status_filters_fancy_progress_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL", "off")
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "message": "Packing layer",
            "lograil.progress.description": "Packing layer",
            "lograil.progress.completed": 3,
            "lograil.progress.total": 4,
        }
    ])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        _patch_tail_log() as mock_log,
        patch("lograil._internal.console.stderr_console.print") as mock_print,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    assert handle.messages == []
    assert mock_log.call_count == 0
    assert mock_print.call_count == 0


def test_tail_to_status_prints_error_with_progress_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "levelname": "ERROR",
            "message": "failed to upload layer",
            "lograil.progress.description": "Uploading layer",
            "lograil.progress.completed": 1,
            "lograil.progress.total": 2,
        }
    ])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        patch("lograil._internal.console.stderr_console.print") as mock_print,
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    assert any("Uploading layer" in str(message) for message in handle.messages)
    printed = [str(call.args[0]) for call in mock_print.call_args_list]
    assert any("failed to upload layer" in message for message in printed)
    assert mock_log.call_count == 0


def test_tail_to_status_can_disable_default_remaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    line = format_progress_line(
        description="Visible progress line", completed=1, total=2
    )
    source = FakeSource([{"message": line}])

    with (
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False, remaps=()),
    ):
        threading.Event().wait(0.05)

    messages = [call.args[1] for call in mock_log.call_args_list]
    assert any("::lograil-progress::" in message for message in messages)


def test_tail_to_status_updates_status_only_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "message": "loading build context",
            "lograil.status_only": True,
        }
    ])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    assert "loading build context" in str(handle.messages[-1])
    assert mock_log.call_count == 0


def test_tail_to_status_does_not_replace_active_progress_with_status_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "message": "building",
            "lograil.progress.description": "building",
            "lograil.progress.completed": 1,
            "lograil.progress.total": 3,
        },
        {
            "message": "step output",
            "lograil.status_only": True,
        },
    ])
    handle = Handle()

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    assert any("building" in str(message) for message in handle.messages)
    assert not any(str(message) == "step output" for message in handle.messages)
    assert mock_log.call_count == 0


def test_tail_to_status_prints_progress_entries_in_plain_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    source = FakeSource([
        {
            "message": "FROM alpine",
            "lograil.progress.description": "FROM alpine",
            "lograil.progress.completed": 0,
            "lograil.progress.total": 2,
        },
        {
            "message": "loading build context",
            "lograil.status_only": True,
        },
    ])

    with (
        patch("lograil._internal.console.stderr_console.print") as mock_print,
        tail_to_status(source=source, delay=0, lossy=False),
    ):
        threading.Event().wait(0.05)

    rendered = [str(call.args[0]) for call in mock_print.call_args_list]
    assert any("FROM alpine" in message for message in rendered)
    assert any("loading build context" in message for message in rendered)


def test_tail_to_status_prints_progress_entries_in_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    configure_logging()
    source = FakeSource([
        {
            "message": "FROM alpine",
            "lograil.progress.description": "FROM alpine",
            "lograil.progress.completed": 0,
            "lograil.progress.total": 2,
        },
        {
            "message": "loading build context",
            "lograil.status_only": True,
        },
    ])

    with tail_to_status(source=source, delay=0, lossy=False):
        threading.Event().wait(0.05)

    rendered = [
        json.loads(line)["message"]
        for line in capsys.readouterr().err.splitlines()
    ]
    assert "FROM alpine" in rendered
    assert "loading build context" in rendered


class SlowSource(LogSource):
    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        _ = query
        yield self._entries(stop=stop)

    def _entries(self, *, stop: threading.Event) -> Iterator[LogEntry]:
        for message in ("first", "second"):
            if stop.is_set():
                return
            time.sleep(0.01)
            yield {"message": message}


class FailingSource(LogSource):
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        _ = stop, query
        self.calls += 1
        yield self._entries()

    def _entries(self) -> Iterator[LogEntry]:
        yield {"message": "before crash"}
        raise self.exc


def test_run_source_to_status_waits_for_generic_source_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        run_source_to_status(SlowSource())

    messages = [str(call.args[0]) for call in mock_print.call_args_list]
    assert any("first" in message for message in messages)
    assert any("second" in message for message in messages)


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("stream failure"), ValueError("read of closed file")],
)
def test_run_source_to_status_reports_source_failure(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    source = FailingSource(exc)

    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        rc = run_source_to_status(source)

    assert rc == 1
    assert source.calls == 1, "a failed stream must not be re-read/replayed"
    messages = [str(call.args[0]) for call in mock_print.call_args_list]
    assert any("before crash" in message for message in messages)


def test_run_source_to_status_terminates_on_persistent_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    class AlwaysFailing(LogSource):
        @contextmanager
        def open(
            self, *, stop: threading.Event, query: LogQuery | None = None
        ) -> Iterator[Iterator[LogEntry]]:
            _ = stop, query
            msg = "backend down"
            raise RuntimeError(msg)
            yield  # pragma: no cover

    start = time.monotonic()
    with (
        patch("lograil._internal.console.stderr_console.print"),
        patch("lograil._internal.log.stderr_console.print"),
    ):
        rc = run_source_to_status(AlwaysFailing())
    elapsed = time.monotonic() - start

    assert rc == 1
    assert elapsed < 5.0, "a persistently failing source must not hang"


def test_tail_to_status_exposes_error_on_drained_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    source = FailingSource(OSError("pipe broke"))

    with (
        patch("lograil._internal.console.stderr_console.print"),
        patch("lograil._internal.log.stderr_console.print"),
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        assert drained.wait(2)

    assert isinstance(drained.error, OSError)


def test_tail_to_status_does_not_dedup_timestampless_repeated_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {"message": "same"},
        {"message": "same"},
    ])

    with (
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    messages = [call.args[1] for call in mock_log.call_args_list]
    assert sum("same" in message for message in messages) == 2


def test_status_only_is_dropped_without_active_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {"message": "loading build context", "lograil.status_only": True}
    ])

    with (
        patch("lograil._internal.log.get_active_status", return_value=None),
        patch("lograil._internal.console.stderr_console.print") as mock_print,
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    # Transient-only text must never be printed permanently; with no
    # spinner to show it on, it is dropped.
    assert mock_log.call_count == 0
    assert mock_print.call_count == 0


def test_progress_renders_when_env_filter_raises_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL", "warning")
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "message": "Packing layer",
            "lograil.progress.description": "Packing layer",
            "lograil.progress.completed": 3,
            "lograil.progress.total": 4,
        }
    ])
    handle = Handle()
    updates: list[object] = []

    with (
        patch("lograil._internal.log.get_active_status", return_value=handle),
        patch(
            "lograil._internal.progress.StatusProgressRenderer.update",
            side_effect=updates.append,
        ),
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    # Progress display is UI state, not log output: raising the filter
    # threshold must not blank the bar (only "off" silences it).
    assert updates
    assert mock_log.call_count == 0


def test_fatal_entries_are_printed_permanently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([{"levelname": "FATAL", "message": "db corrupted"}])

    with (
        patch("lograil._internal.console.stderr_console.print") as mock_print,
        _patch_tail_log() as mock_log,
        tail_to_status(source=source, delay=0, lossy=False) as drained,
    ):
        drained.wait(1)

    printed = [str(call.args[0]) for call in mock_print.call_args_list]
    assert any("db corrupted" in message for message in printed)
    assert mock_log.call_count == 0


def test_print_tail_entry_uses_stderr_console_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 40)
    monkeypatch.setattr(console.stderr_console, "width", 200)
    message = ("word " * 20).strip()

    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        _print_tail_entry({"message": message}, show_context=False)

    rendered = str(mock_print.call_args.args[0])
    assert "\n" not in rendered


def test_progress_detail_with_brackets_does_not_fail_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    source = FakeSource([
        {
            "lograil.progress.description": "copy [/etc/passwd] done",
            "lograil.progress.completed": 1,
            "lograil.progress.total": 1,
        }
    ])

    with tail_to_status(source=source, delay=0, lossy=False) as drained:
        assert drained.wait(1)

    assert drained.error is None
