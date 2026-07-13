# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest
import time_machine

from lograil import LogEntry
from lograil.sources.fd import FileDescriptorLogSource

_FROZEN_AT = dt.datetime(2026, 7, 21, tzinfo=dt.timezone.utc)
_FROZEN_TIMESTAMP = _FROZEN_AT.timestamp()

# Generous budget for loaded CI machines; the happy path never waits
# anywhere near this long.
_WAIT_TIMEOUT = 10.0


@pytest.fixture(autouse=True)
def frozen_time() -> Iterator[None]:
    with time_machine.travel(_FROZEN_AT, tick=False):
        yield


@dataclass
class _SourceRun:
    entries: list[LogEntry]
    errors: list[BaseException]
    thread: threading.Thread
    condition: threading.Condition


@contextmanager
def _running_source(
    source: FileDescriptorLogSource,
    stop: threading.Event,
    *,
    expect_error: bool = False,
) -> Iterator[_SourceRun]:
    entries: list[LogEntry] = []
    errors: list[BaseException] = []
    condition = threading.Condition()

    def target() -> None:
        try:
            with source.open(stop=stop) as handle:
                for entry in handle:
                    with condition:
                        entries.append(entry)
                        condition.notify_all()
        except BaseException as exc:  # noqa: BLE001 - thread reports to test
            with condition:
                errors.append(exc)
                condition.notify_all()

    thread = threading.Thread(target=target)
    thread.start()
    run = _SourceRun(
        entries=entries,
        errors=errors,
        thread=thread,
        condition=condition,
    )
    try:
        yield run
    finally:
        # Stop and join even when the test body fails, so a live thread
        # never outlives the test to race on closed or reused fds.
        stop.set()
        thread.join(timeout=_WAIT_TIMEOUT)
    assert not thread.is_alive(), "source thread failed to stop"
    if not expect_error:
        assert not errors, f"unexpected source errors: {errors!r}"


def _wait_for_count(run: _SourceRun, count: int) -> None:
    with run.condition:
        if run.condition.wait_for(
            lambda: len(run.entries) >= count or bool(run.errors),
            _WAIT_TIMEOUT,
        ):
            if run.errors:
                msg = f"source failed before {count} entries: {run.errors!r}"
                raise AssertionError(msg)
            return
    msg = f"expected at least {count} entries, got {run.entries!r}"
    raise AssertionError(msg)


def _wait_for_error(run: _SourceRun) -> None:
    with run.condition:
        if run.condition.wait_for(lambda: bool(run.errors), _WAIT_TIMEOUT):
            return
    msg = "expected source error"
    raise AssertionError(msg)


def _track_source_dup(
    monkeypatch: pytest.MonkeyPatch, read_fd: int
) -> list[int]:
    """Record the fds the source dups from ``read_fd``.

    Lets read fakes key on the source's actual fd instead of matching
    every fd other than ``read_fd``, which any stray reader would satisfy.
    """
    source_fds: list[int] = []
    real_dup = os.dup

    def tracked_dup(fd: int) -> int:
        duped = real_dup(fd)
        if fd == read_fd:
            source_fds.append(duped)
        return duped

    monkeypatch.setattr(os, "dup", tracked_dup)
    return source_fds


def _payloads(entries: list[LogEntry]) -> list[dict[str, object]]:
    for entry in entries:
        assert entry["created"] == _FROZEN_TIMESTAMP
    return [
        {key: value for key, value in entry.items() if key != "created"}
        for entry in entries
    ]


def test_file_descriptor_source_reads_fd_lines() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe"), stop
        ) as run:
            os.write(write_fd, b"one\ntwo\n")
            _wait_for_count(run, 2)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [
        {"message": "one", "name": "pipe"},
        {"message": "two", "name": "pipe"},
    ]


def test_file_descriptor_source_reads_io_object_lines() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    with os.fdopen(read_fd, "rb", closefd=True) as reader:
        try:
            with _running_source(
                FileDescriptorLogSource(reader, name="io"), stop
            ) as run:
                os.write(write_fd, b"ready\n")
                _wait_for_count(run, 1)
        finally:
            os.close(write_fd)

    assert _payloads(run.entries) == [{"message": "ready", "name": "io"}]


def test_integer_fd_remains_owned_by_caller_after_source_closes() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    stat_before = os.fstat(read_fd)
    try:
        with _running_source(FileDescriptorLogSource(read_fd), stop) as run:
            os.write(write_fd, b"first\n")
            _wait_for_count(run, 1)

        # The fd must still refer to the same pipe (not closed, not a
        # reused number) and remain usable by the caller.
        stat_after = os.fstat(read_fd)
        assert (stat_after.st_dev, stat_after.st_ino) == (
            stat_before.st_dev,
            stat_before.st_ino,
        )
        assert os.get_inheritable(read_fd) is False
        os.write(write_fd, b"still-ours\n")
        assert os.read(read_fd, 64) == b"still-ours\n"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_partial_trailing_line_is_emitted_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    real_read = os.read
    read_partial = threading.Event()

    def tracked_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if chunk == b"partial":
            read_partial.set()
        return chunk

    monkeypatch.setattr(os, "read", tracked_read)
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe"), stop
        ) as run:
            os.write(write_fd, b"partial")
            assert read_partial.wait(_WAIT_TIMEOUT)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [{"message": "partial", "name": "pipe"}]


def test_full_chunk_read_returns_to_select_before_next_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    real_read = os.read
    read_full_chunk = threading.Event()

    def tracked_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        if chunk == b"abcd":
            read_full_chunk.set()
        return chunk

    monkeypatch.setattr(os, "read", tracked_read)
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe", chunk_size=4), stop
        ) as run:
            os.write(write_fd, b"abcd")
            assert read_full_chunk.wait(_WAIT_TIMEOUT)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [{"message": "abcd", "name": "pipe"}]


def test_crlf_across_chunk_boundary_emits_no_empty_entry() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe", chunk_size=6), stop
        ) as run:
            os.write(write_fd, b"hello\r\nworld\r\n")
            _wait_for_count(run, 2)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [
        {"message": "hello", "name": "pipe"},
        {"message": "world", "name": "pipe"},
    ]


def test_blocking_io_error_returns_to_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    real_read = os.read
    source_fds = _track_source_dup(monkeypatch, read_fd)
    raised = threading.Event()

    def flaky_read(fd: int, size: int) -> bytes:
        if fd in source_fds and not raised.is_set():
            raised.set()
            raise BlockingIOError
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", flaky_read)
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe"), stop
        ) as run:
            os.write(write_fd, b"after\n")
            _wait_for_count(run, 1)
            assert raised.is_set()
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [{"message": "after", "name": "pipe"}]


def test_file_descriptor_source_terminates_on_eof() -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe"), stop
        ) as run:
            os.write(write_fd, b"last")
            os.close(write_fd)
            write_fd = -1
            # EOF alone must terminate the thread; stop is not set here.
            run.thread.join(timeout=_WAIT_TIMEOUT)
            assert not run.thread.is_alive()
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)

    assert _payloads(run.entries) == [{"message": "last", "name": "pipe"}]


def test_read_error_is_raised_and_partial_line_flushed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_fd, write_fd = os.pipe()
    stop = threading.Event()
    real_read = os.read
    source_fds = _track_source_dup(monkeypatch, read_fd)
    fail = threading.Event()

    def failing_read(fd: int, size: int) -> bytes:
        if fd in source_fds and fail.is_set():
            raise OSError(5, "Input/output error")
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", failing_read)
    try:
        with _running_source(
            FileDescriptorLogSource(read_fd, name="pipe"),
            stop,
            expect_error=True,
        ) as run:
            os.write(write_fd, b"whole\npartial")
            _wait_for_count(run, 1)
            fail.set()
            os.write(write_fd, b"x")
            _wait_for_error(run)
    finally:
        os.close(read_fd)
        os.close(write_fd)

    assert _payloads(run.entries) == [
        {"message": "whole", "name": "pipe"},
        {"message": "partial", "name": "pipe"},
    ]
    assert len(run.errors) == 1
    assert isinstance(run.errors[0], RuntimeError)
