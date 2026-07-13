# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""End-to-end tests exercising public flows across module boundaries.

These tests cover the integration seams that unit tests repeatedly missed:
real pipes and files with awkward chunk boundaries, subprocess lifecycles,
an actual HTTP server for the VictoriaLogs tail protocol, fancy-mode output
with hostile input, and a simulated base install without optional extras.
"""

from __future__ import annotations

from typing import Any, ClassVar

import datetime as dt
import json
import logging
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest
import time_machine

from lograil import (
    configure_logging,
    status,
    stream_log_files,
    update_status,
)
from lograil._internal.tail import run_source_to_status, tail_to_status
from lograil.sources.fd import FileDescriptorLogSource
from lograil.sources.file import FileLogSource
from lograil.sources.victoria import (
    VictoriaLogsSource,
    VictoriaLogsStreamError,
)


class _FastStop(threading.Event):
    """Event whose timed waits are shortened to keep backoffs fast."""

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is not None:
            timeout = min(timeout, 0.02)
        return super().wait(timeout)


def _printed_messages(mock_print: Any) -> list[str]:
    return [str(call.args[0]) for call in mock_print.call_args_list]


# ---------------------------------------------------------------------------
# Pipes: chunk boundaries, CRLF, unterminated final lines
# ---------------------------------------------------------------------------


def _run_fd_source(chunks: list[bytes], *, gap: float = 0.01) -> list[str]:
    read_fd, write_fd = os.pipe()

    def _write() -> None:
        for chunk in chunks:
            os.write(write_fd, chunk)
            time.sleep(gap)
        os.close(write_fd)

    writer = threading.Thread(target=_write, daemon=True)
    writer.start()
    source = FileDescriptorLogSource(read_fd)
    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        rc = run_source_to_status(source)
    writer.join(timeout=5)
    assert rc == 0
    return _printed_messages(mock_print)


def test_e2e_fd_crlf_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    messages = _run_fd_source([b"one\r", b"\ntwo\r\n"])

    joined = [m for m in messages if "one" in m or "two" in m]
    assert len(joined) == 2, messages
    assert all(m.strip() for m in messages), (
        f"phantom empty entry rendered: {messages!r}"
    )


def test_e2e_fd_unterminated_final_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    messages = _run_fd_source([b"first\nlast-no-newline"])

    assert any("first" in m for m in messages)
    assert any("last-no-newline" in m for m in messages)


def test_e2e_fd_form_feed_does_not_split_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    messages = _run_fd_source([b"page1\x0cpage2\n"])

    assert any("page1\x0cpage2" in m for m in messages), messages


# ---------------------------------------------------------------------------
# Files: no trailing newline, tail_lines backlog
# ---------------------------------------------------------------------------


def _collect_file_entries(
    source: FileLogSource,
    *,
    expected: int,
    append: tuple[Path, str] | None = None,
    timeout: float = 1.0,
) -> list[str]:
    stop = threading.Event()
    collected: list[str] = []
    lock = threading.Lock()

    def _consume() -> None:
        with source.open(stop=stop) as entries:
            for entry in entries:
                with lock:
                    collected.append(str(entry.get("message", "")))

    consumer = threading.Thread(target=_consume, daemon=True)
    consumer.start()
    deadline = time.monotonic() + timeout
    appended = append is None
    while time.monotonic() < deadline:
        with lock:
            count = len(collected)
        if not appended and count >= expected - 1:
            path, text = append  # type: ignore[misc]
            with path.open("a") as handle:
                handle.write(text)
            appended = True
        if count >= expected:
            break
        time.sleep(0.01)
    stop.set()
    consumer.join(timeout=5)
    with lock:
        return list(collected)


def test_e2e_file_final_line_without_newline_is_emitted(
    tmp_path: Path,
) -> None:
    log_file = tmp_path / "app.log"
    log_file.write_text("first\nlast message")
    source = FileLogSource(
        [log_file], read_from="beginning", poll_interval=0.01
    )

    collected = _collect_file_entries(source, expected=1)

    assert "first" in collected
    assert "last message" in collected


def test_e2e_file_tail_lines_backlog_then_follow(tmp_path: Path) -> None:
    log_file = tmp_path / "app.log"
    log_file.write_text("l1\nl2\nl3\nl4\nl5\n")
    source = FileLogSource(
        [log_file], read_from="end", tail_lines=2, poll_interval=0.01
    )

    collected = _collect_file_entries(
        source, expected=3, append=(log_file, "l6\n")
    )

    assert collected[:2] == ["l4", "l5"], collected
    assert "l6" in collected
    assert not any(m in collected for m in ("l1", "l2", "l3"))


# ---------------------------------------------------------------------------
# Failure semantics end-to-end
# ---------------------------------------------------------------------------


def test_e2e_source_failure_is_reported_not_hung(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    class ExplodingSource(FileDescriptorLogSource):
        @contextmanager
        def open(
            self,
            *,
            stop: threading.Event,
            query: Any = None,
        ) -> Iterator[Iterator[dict[str, object]]]:
            _ = stop, query
            yield self._entries()

        def _entries(self) -> Iterator[dict[str, object]]:
            yield {"message": "one entry"}
            msg = "backend exploded"
            raise ConnectionError(msg)

    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    start = time.monotonic()
    with patch("lograil._internal.console.stderr_console.print"):
        rc = run_source_to_status(ExplodingSource(read_fd))
    elapsed = time.monotonic() - start
    os.close(read_fd)

    assert rc == 1
    assert elapsed < 5.0


# ---------------------------------------------------------------------------
# Fancy mode with hostile input
# ---------------------------------------------------------------------------


def test_e2e_fancy_status_with_markup_hostile_messages(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    logger = logging.getLogger("lograil.e2e")

    with status("working", done="done"):
        logger.info("failed to parse [/etc/config]")
        logger.warning("disk [bold]almost full[/bold]")

    err = capsys.readouterr().err
    assert "almost full" in err, "deferred warning must flush on success"
    assert "\\[bold]" not in err, "no visible escape backslashes expected"


def test_e2e_fancy_nested_status_and_update_do_not_leak_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    with status("working"):
        update_status(subject="step 2")
    with status("another task"):
        pass

    err = capsys.readouterr().err
    for line in err.splitlines():
        if "another task" in line:
            assert "step 2" not in line, f"sticky prefix leaked: {line!r}"


# ---------------------------------------------------------------------------
# VictoriaLogs against a real HTTP server
# ---------------------------------------------------------------------------


def _ndjson(entries: list[dict[str, str]]) -> bytes:
    return "\n".join(json.dumps(entry) for entry in entries).encode() + b"\n"


class _VictoriaHandler(BaseHTTPRequestHandler):
    script: ClassVar[list[tuple[int, list[dict[str, str]]]]] = []
    requests_seen: ClassVar[list[str]] = []
    lock: ClassVar[threading.Lock] = threading.Lock()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode()
        with self.lock:
            self.requests_seen.append(body)
            index = len(self.requests_seen) - 1
        step = min(index, len(self.script) - 1)
        code, entries = self.script[step]
        payload = (
            _ndjson(entries) if code == 200 else b"synthetic backend error"
        )
        self.send_response(code)
        self.send_header("Content-Type", "application/stream+json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args: Any) -> None:
        _ = args


@pytest.fixture
def victoria_server() -> Any:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _VictoriaHandler)
    _VictoriaHandler.script = []
    _VictoriaHandler.requests_seen = []
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01),
        daemon=True,
    )
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=5)


def test_e2e_victoria_recovers_from_503_and_dedups_seam_only(
    victoria_server: ThreadingHTTPServer,
) -> None:
    t_same = "2026-07-20T10:00:00Z"
    t_newer = "2026-07-20T10:00:00.500Z"
    _VictoriaHandler.script = [
        (503, []),
        (
            200,
            [
                {"_msg": "e1", "_time": t_same},
                {"_msg": "e2", "_time": t_same},
            ],
        ),
        (
            200,
            [
                {"_msg": "e2", "_time": t_same},
                {"_msg": "e3", "_time": t_newer},
            ],
        ),
        (200, []),
    ]
    base_url = f"http://127.0.0.1:{victoria_server.server_address[1]}"
    source = VictoriaLogsSource(base_url=base_url)
    stop = _FastStop()
    collected: list[str] = []

    with source.open(stop=stop) as entries:
        for entry in entries:
            collected.append(str(entry.get("message")))
            if len(collected) >= 3:
                stop.set()

    assert collected == ["e1", "e2", "e3"], (
        "same-timestamp siblings on one connection must both arrive; the "
        "seam replay of e2 must be dropped exactly once"
    )
    assert len(_VictoriaHandler.requests_seen) >= 3
    assert "start=" in _VictoriaHandler.requests_seen[2], (
        "reconnect must resume from the delivered cursor"
    )


def test_e2e_victoria_fatal_http_error_raises_descriptive_error(
    victoria_server: ThreadingHTTPServer,
) -> None:
    _VictoriaHandler.script = [(400, [])]
    base_url = f"http://127.0.0.1:{victoria_server.server_address[1]}"
    source = VictoriaLogsSource(base_url=base_url)

    with (
        pytest.raises(VictoriaLogsStreamError, match="HTTP 400"),
        source.open(stop=_FastStop()) as entries,
    ):
        for _ in entries:
            pass


def test_e2e_victoria_failure_reported_by_run_source_to_status(
    victoria_server: ThreadingHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    _VictoriaHandler.script = [(400, [])]
    base_url = f"http://127.0.0.1:{victoria_server.server_address[1]}"

    with (
        patch("lograil._internal.console.stderr_console.print"),
        patch("lograil._internal.log.stderr_console.print"),
    ):
        rc = run_source_to_status(VictoriaLogsSource(base_url=base_url))

    assert rc == 1


# ---------------------------------------------------------------------------
# Base install (no extras)
# ---------------------------------------------------------------------------


def test_e2e_stream_log_files_without_watchdog_extra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "watchdog",
        "watchdog.events",
        "watchdog.observers",
        "watchdog.observers.polling",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "lograil.sources.file", raising=False)

    with pytest.raises(RuntimeError, match=r"lograil\[file\]"):
        stream_log_files([tmp_path / "app.log"], tail=10)


# ---------------------------------------------------------------------------
# Tail pipeline drains before teardown
# ---------------------------------------------------------------------------


def test_e2e_slow_source_output_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    read_fd, write_fd = os.pipe()

    frozen_at = dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc)
    with time_machine.travel(frozen_at, tick=False) as traveller:

        def _write() -> None:
            for index in range(5):
                traveller.shift(0.15)
                os.write(write_fd, f"slow-{index}\n".encode())
            os.close(write_fd)

        writer = threading.Thread(target=_write, daemon=True)
        writer.start()
        with patch(
            "lograil._internal.console.stderr_console.print"
        ) as mock_print:
            rc = run_source_to_status(FileDescriptorLogSource(read_fd))
        writer.join(timeout=5)

    assert rc == 0
    messages = _printed_messages(mock_print)
    for index in range(5):
        assert any(f"slow-{index}" in m for m in messages), (
            f"entry slow-{index} truncated: {messages!r}"
        )


def test_e2e_tail_to_status_stop_is_honored_quickly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    read_fd, write_fd = os.pipe()
    try:
        start = time.monotonic()
        with tail_to_status(source=FileDescriptorLogSource(read_fd), delay=0):
            time.sleep(0.1)
        elapsed = time.monotonic() - start
        assert elapsed < 3.0, "teardown must not hang on an idle pipe"
    finally:
        os.close(write_fd)
        os.close(read_fd)
