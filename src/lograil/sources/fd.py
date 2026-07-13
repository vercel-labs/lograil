# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""File descriptor log source for lograil."""

from __future__ import annotations

from typing import BinaryIO, TextIO

import os
import select
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from lograil._internal.lines import flush_remainder, split_byte_lines
from lograil._internal.tail import LogEntry, LogQuery, LogSource

__all__ = ["FileDescriptorLogSource"]

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes
    import msvcrt

    _ERROR_BROKEN_PIPE = 109
    _ERROR_INVALID_HANDLE = 6

    def _pipe_has_data(fd: int) -> bool | None:
        handle = msvcrt.get_osfhandle(fd)
        available = ctypes.wintypes.DWORD()
        ok = ctypes.windll.kernel32.PeekNamedPipe(
            ctypes.wintypes.HANDLE(handle),
            None,
            0,
            None,
            ctypes.byref(available),
            None,
        )
        if ok:
            return available.value > 0
        error = ctypes.windll.kernel32.GetLastError()
        if error == _ERROR_BROKEN_PIPE:
            return None
        if error == _ERROR_INVALID_HANDLE:
            msg = f"invalid Windows pipe fd {fd}"
            raise RuntimeError(msg)
        msg = f"PeekNamedPipe() failed for fd {fd}: WinError {error}"
        raise RuntimeError(msg)

    def _wait_for_data(
        fd: int, *, stop: threading.Event, poll_interval: float
    ) -> bool:
        while not stop.is_set():
            has_data = _pipe_has_data(fd)
            if has_data is None or has_data:
                return True
            stop.wait(poll_interval)
        return False

else:

    def _wait_for_data(
        fd: int, *, stop: threading.Event, poll_interval: float
    ) -> bool:
        while not stop.is_set():
            try:
                readable, _, _ = select.select([fd], [], [], poll_interval)
            except (OSError, ValueError) as exc:
                # A select failure (e.g. fd >= FD_SETSIZE) is a stream
                # error, not end-of-stream; fail loudly instead of
                # reporting a clean drain with no data.
                msg = f"select() failed for fd {fd}: {exc}"
                raise RuntimeError(msg) from exc
            if readable:
                return True
        return False


@dataclass
class FileDescriptorLogSource(LogSource, source_id="fd"):
    """Read newline-delimited log entries from a file descriptor."""

    fd: int | TextIO | BinaryIO
    name: str = "file"
    encoding: str = "utf-8"
    poll_interval: float = 0.05
    chunk_size: int = 8192

    @classmethod
    def from_stdin(cls, stdin: TextIO) -> FileDescriptorLogSource:
        """Create a source that reads from ``stdin``."""
        return cls(fd=stdin, name="stdin")

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        """Open the descriptor and yield an iterable entry handle."""
        _ = query
        if isinstance(self.fd, int):
            owned_fd = os.dup(self.fd)
            close_fd = True
        else:
            owned_fd = self.fd.fileno()
            close_fd = False
        try:
            yield self._read_fd(owned_fd, stop=stop)
        finally:
            if close_fd:
                os.close(owned_fd)

    def _read_fd(self, fd: int, *, stop: threading.Event) -> Iterator[LogEntry]:
        buffer = b""
        while not stop.is_set():
            ready = _wait_for_data(
                fd,
                stop=stop,
                poll_interval=self.poll_interval,
            )
            if not ready:
                break
            try:
                chunk = os.read(fd, self.chunk_size)
            except BlockingIOError:
                continue
            except OSError as exc:
                # A read failure (e.g. EIO from a pty) is a stream error,
                # not end-of-stream; deliver the buffered partial line and
                # fail loudly instead of reporting a clean drain.
                for raw_line in flush_remainder(buffer):
                    yield self._entry(raw_line)
                msg = f"read() failed for fd {fd}: {exc}"
                raise RuntimeError(msg) from exc
            if not chunk:
                for raw_line in flush_remainder(buffer):
                    yield self._entry(raw_line)
                return
            lines, buffer = split_byte_lines(buffer, chunk)
            for raw_line in lines:
                yield self._entry(raw_line)
        for raw_line in flush_remainder(buffer):
            yield self._entry(raw_line)

    def _entry(self, raw_line: bytes) -> LogEntry:
        return {
            "message": raw_line.decode(self.encoding, errors="replace"),
            "name": self.name,
            "created": time.time(),
        }
