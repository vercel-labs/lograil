# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""File path and glob log source for lograil."""

from __future__ import annotations

from typing import BinaryIO, Protocol, TypeAlias, overload

import codecs
import glob
import os
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers.polling import PollingObserver as Observer

from lograil._internal.lines import flush_remainder, split_text_lines
from lograil._internal.tail import LogEntry, LogQuery, LogSource

__all__ = ["FileLogSource"]


_Identity = tuple[int, int] | None
_PathInput: TypeAlias = str | os.PathLike[str]
_READ_CHUNK_SIZE = 64 * 1024
_TAIL_BLOCK_SIZE = 8192
_PREFIX_PROBE_SIZE = 64


class _ClosableIterator(Iterator[LogEntry], Protocol):
    def close(self) -> None: ...


@overload
def _normalize_paths(paths: _PathInput) -> Sequence[_PathInput]: ...


@overload
def _normalize_paths(paths: Sequence[_PathInput]) -> Sequence[_PathInput]: ...


def _normalize_paths(
    paths: _PathInput | Sequence[_PathInput],
) -> Sequence[_PathInput]:
    if isinstance(paths, str):
        return [paths]
    if isinstance(paths, os.PathLike):
        return [os.fspath(paths)]
    return paths


@dataclass
class _OpenFile:
    path: Path
    identity: _Identity
    # The handle is binary and offsets are true byte positions comparable
    # to ``st_size``; decoding happens through the incremental decoder so a
    # chunk boundary may split a multibyte sequence safely.
    handle: BinaryIO
    offset: int
    decoder: codecs.IncrementalDecoder
    buffer: str = ""
    # First bytes of the file as last observed; append-only growth keeps
    # the stored value a prefix of the current content, so a mismatch means
    # the file was rewritten in place (copytruncate) even when it regrew
    # past the old offset between polls.
    prefix: bytes = b""


class _WakeHandler(FileSystemEventHandler):
    def __init__(self, wake: threading.Event) -> None:
        self._wake = wake

    def on_any_event(self, event: FileSystemEvent) -> None:
        _ = event
        self._wake.set()


class FileLogSource(LogSource, source_id="file"):
    """Read newline-delimited entries from local files and glob patterns."""

    def __init__(
        self,
        paths: _PathInput | Sequence[_PathInput],
        *,
        read_from: str = "end",
        name: str | None = None,
        encoding: str = "utf-8",
        poll_interval: float = 0.1,
        tail_lines: int | None = None,
    ) -> None:
        """Initialize the source with file paths or glob patterns.

        Relative paths and glob patterns are anchored to the working
        directory captured at construction time; a later ``os.chdir`` does
        not change which files are discovered.

        ``tail_lines`` is a backlog size for ``read_from='end'``: on the
        initial open of each initially-present file, the last ``tail_lines``
        complete lines are emitted first (instead of starting at end of
        file) before tailing continues.  Files discovered or rotated later
        are new content and are read from the beginning regardless.  It has
        no effect when ``read_from='beginning'``.
        """
        if read_from not in {"beginning", "end"}:
            msg = "read_from must be 'beginning' or 'end'"
            raise ValueError(msg)
        if tail_lines is not None and tail_lines < 0:
            msg = "tail_lines must be a non-negative integer or None"
            raise ValueError(msg)
        raw_paths = _normalize_paths(paths)
        self._anchor = Path.cwd()
        self._patterns = [self._anchored(Path(path)) for path in raw_paths]
        self._read_from = read_from
        self._tail_lines = tail_lines
        self._name = name
        self._encoding = encoding
        self._poll_interval = poll_interval
        self._initial_paths = set(self._discover_paths())

    def _anchored(self, pattern: Path) -> Path:
        if pattern.is_absolute():
            return pattern
        return self._anchor / pattern

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        """Open configured files and yield an iterable entry handle."""
        _ = query
        entries = self._read_entries(stop=stop)
        try:
            yield entries
        finally:
            entries.close()

    def _read_entries(self, *, stop: threading.Event) -> _ClosableIterator:
        wake = threading.Event()
        observer = Observer()
        scheduled = self._schedule_observers(observer, wake)
        if scheduled:
            observer.start()
        open_files: dict[Path, _OpenFile] = {}
        try:
            yield from self._reconcile(
                open_files,
                initial_existing=self._initial_paths,
            )
            while not stop.is_set():
                yield from self._read_available(open_files)
                yield from self._reconcile(
                    open_files,
                    initial_existing=None,
                )
                yield from self._read_available(open_files)
                wake.wait(self._poll_interval)
                wake.clear()
            for state in open_files.values():
                yield from self._flush_buffer(state)
        finally:
            for state in open_files.values():
                self._close(state)
            if scheduled:
                observer.stop()
                observer.join(timeout=2.0)

    def _schedule_observers(
        self, observer: Observer, wake: threading.Event
    ) -> bool:
        handler = _WakeHandler(wake)
        watched: set[Path] = set()
        for pattern in self._patterns:
            directory = self._watch_directory(pattern)
            if directory in watched:
                continue
            watched.add(directory)
            try:
                observer.schedule(handler, str(directory), recursive=False)
            except OSError:
                continue
        return bool(watched)

    def _watch_directory(self, pattern: Path) -> Path:
        if pattern.exists() and pattern.is_dir():
            parent = pattern
        elif glob.has_magic(str(pattern)):
            parent = self._literal_parent(pattern)
        else:
            parent = pattern.parent
        if str(parent) in {"", "."}:
            return self._anchor
        return parent

    def _reconcile(
        self,
        open_files: dict[Path, _OpenFile],
        *,
        initial_existing: set[Path] | None,
    ) -> Iterator[LogEntry]:
        current_paths = self._discover_paths()
        for path in current_paths:
            try:
                stat_result = path.stat()
            except OSError:
                continue
            identity = self._identity(stat_result)
            state = open_files.get(path)
            if state is None:
                start = (
                    self._read_from
                    if initial_existing is not None and path in initial_existing
                    else "beginning"
                )
                opened = self._open(path, identity, stat_result.st_size, start)
                if opened is not None:
                    open_files[path] = opened
                continue
            if state.identity != identity:
                yield from self._read_state(state)
                yield from self._flush_buffer(state)
                self._close(state)
                # The closed state must not linger in open_files: when the
                # reopen fails, the next pass rediscovers the path instead
                # of reading from a closed handle.
                del open_files[path]
                opened = self._open(
                    path, identity, stat_result.st_size, "beginning"
                )
                if opened is not None:
                    open_files[path] = opened
                continue
            if self._rewritten(state, stat_result.st_size):
                state.handle.seek(0)
                state.offset = 0
                state.buffer = ""
                state.decoder.reset()

    def _rewritten(self, state: _OpenFile, size: int) -> bool:
        """Detect same-inode truncation, including truncate-then-regrow.

        A plain shrink (``size < offset``) is the easy case; a copytruncate
        rotation where the file regrows past the old offset between polls
        is caught by probing the file's first bytes, which append-only
        growth can never change.
        """
        pos = state.handle.tell()
        state.handle.seek(0)
        current = state.handle.read(_PREFIX_PROBE_SIZE)
        state.handle.seek(pos)
        if size < state.offset or not current.startswith(state.prefix):
            state.prefix = current
            return True
        if len(current) > len(state.prefix):
            state.prefix = current
        return False

    def _discover_paths(self) -> list[Path]:
        paths: set[Path] = set()
        for pattern in self._patterns:
            for path in self._expand_pattern(pattern):
                if path.is_file():
                    paths.add(path.resolve())
        return sorted(paths)

    def _expand_pattern(self, pattern: Path) -> Iterator[Path]:
        pattern_text = str(pattern)
        if not glob.has_magic(pattern_text):
            yield pattern
            return
        parent = self._literal_parent(pattern)
        try:
            relative_pattern = pattern.relative_to(parent)
        except ValueError:
            yield from (
                (parent / path).resolve()
                for path in glob.glob(  # ruff:ignore[glob]
                    pattern_text,
                    recursive=True,
                )
            )
            return
        yield from parent.glob(str(relative_pattern))

    def _literal_parent(self, pattern: Path) -> Path:
        parts = pattern.parts
        literal_parts: list[str] = []
        for part in parts:
            if glob.has_magic(part):
                break
            literal_parts.append(part)
        if not literal_parts:
            return self._anchor
        parent = Path(*literal_parts)
        if parent.suffix and parent.name == pattern.name:
            parent = parent.parent
        if str(parent) in {"", "."}:
            return self._anchor
        return parent

    def _open(
        self, path: Path, identity: _Identity, size: int, read_from: str
    ) -> _OpenFile | None:
        try:
            handle = path.open("rb")
        except OSError:
            return None
        if read_from != "end":
            offset = 0
        elif self._tail_lines is not None:
            offset = self._tail_start_offset(path, size, self._tail_lines)
        else:
            offset = size
        prefix = handle.read(_PREFIX_PROBE_SIZE)
        handle.seek(offset)
        decoder = codecs.getincrementaldecoder(self._encoding)(errors="replace")
        return _OpenFile(
            path=path,
            identity=identity,
            handle=handle,
            offset=offset,
            decoder=decoder,
            prefix=prefix,
        )

    def _tail_start_offset(self, path: Path, size: int, count: int) -> int:
        """Find the offset of the ``count``-th-from-last complete line.

        Scans backwards from end of file in bounded blocks counting
        newlines; returns the byte offset from which reading yields at most
        the last ``count`` complete lines (plus any unterminated tail).
        """
        try:
            with path.open("rb") as handle:
                return self._scan_back_for_lines(handle, size, count)
        except OSError:
            return size

    def _scan_back_for_lines(
        self, handle: BinaryIO, size: int, count: int
    ) -> int:
        needed = count + 1
        pos = size
        while pos > 0 and needed > 0:
            read_size = min(_TAIL_BLOCK_SIZE, pos)
            pos -= read_size
            handle.seek(pos)
            block = handle.read(read_size)
            end = len(block)
            while needed > 0:
                index = block.rfind(b"\n", 0, end)
                if index < 0:
                    break
                needed -= 1
                if needed == 0:
                    return pos + index + 1
                end = index
        return 0

    def _read_available(
        self, open_files: dict[Path, _OpenFile]
    ) -> Iterator[LogEntry]:
        for state in list(open_files.values()):
            yield from self._read_state(state)

    def _read_state(self, state: _OpenFile) -> Iterator[LogEntry]:
        while True:
            chunk = state.handle.read(_READ_CHUNK_SIZE)
            if not chunk:
                return
            state.offset = state.handle.tell()
            text = state.decoder.decode(chunk)
            lines, state.buffer = split_text_lines(state.buffer, text)
            for line in lines:
                yield self._entry(state.path, line)

    def _flush_buffer(self, state: _OpenFile) -> Iterator[LogEntry]:
        remainder = state.buffer + state.decoder.decode(b"", final=True)
        state.buffer = ""
        for line in flush_remainder(remainder):
            yield self._entry(state.path, line)

    def _entry(self, path: Path, line: str) -> LogEntry:
        return {
            "message": line,
            "name": self._name or str(path),
            "created": time.time(),
        }

    def _close(self, state: _OpenFile) -> None:
        close = state.handle.close
        close()

    def _identity(self, stat_result: os.stat_result) -> _Identity:
        ino = getattr(stat_result, "st_ino", 0)
        dev = getattr(stat_result, "st_dev", 0)
        if ino == 0 and dev == 0:
            return None
        return (dev, ino)
