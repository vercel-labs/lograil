# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Async log source primitives."""

from __future__ import annotations

from typing import ClassVar, Literal

import contextlib
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

import anyio
from anyio.abc import ByteReceiveStream, Process, TaskGroup
from anyio.lowlevel import checkpoint
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from lograil._internal.lines import flush_remainder, split_byte_lines
from lograil._internal.registry import SourceRegistryBase
from lograil._internal.tail import LogEntry, LogQuery

StreamMode = Literal["stderr", "stdout", "combined"]
_PIPE = -1
_STDOUT = -2
_DEVNULL = -3
_CLEANUP_WAIT = 2.0


class SubprocessStartError(Exception):
    """Raised when a subprocess cannot be started."""


class AsyncLogSource(SourceRegistryBase):
    """Base class for async backends that read structured log entries."""

    _registry: ClassVar[dict[str, type[AsyncLogSource]]] = {}
    _registry_label: ClassVar[str] = "async log source"

    def open(
        self,
        query: LogQuery | None = None,
    ) -> contextlib.AbstractAsyncContextManager[AsyncIterable[LogEntry]]:
        """Open the source and return an async-iterable entry handle."""
        _ = query
        msg = f"{type(self).__name__} does not implement open"
        raise NotImplementedError(msg)


@dataclass
class SubprocessLogSource(AsyncLogSource, source_id="subprocess"):
    """Async log source backed by a subprocess stream.

    ``open()`` starts the child and returns an async-iterable handle.  The
    context manager owns the child, pipe pumps, and cleanup; callers must
    iterate the handle inside the ``async with`` block so AnyIO task groups
    remain properly scoped on asyncio and Trio backends.
    """

    argv: Sequence[str]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    name: str | None = None
    subject: str | None = None
    category: str | None = None
    stream: StreamMode = "stderr"
    kind: str | None = None
    cleanup_wait: float = _CLEANUP_WAIT
    exit_code: int | None = field(default=None, init=False)

    @asynccontextmanager
    async def open(
        self,
        query: LogQuery | None = None,
    ) -> AsyncIterator[SubprocessLogHandle]:
        """Start the subprocess and yield an async-iterable log handle."""
        _ = query
        if self.stream not in {"stderr", "stdout", "combined"}:
            msg = f"unknown subprocess stream mode: {self.stream}"
            raise ValueError(msg)
        # "combined" merges stderr into stdout at the fd level (one pipe),
        # preserving the child's relative write order across the two
        # streams -- two independently pumped pipes would interleave
        # arbitrarily.  Entries are labeled "combined": per-stream
        # attribution is lost in the merge, as with ``2>&1``.
        combined = self.stream == "combined"
        want_stdout = self.stream == "stdout" or combined
        want_stderr = self.stream == "stderr"
        try:
            process = await anyio.open_process(
                list(self.argv),
                stdin=_DEVNULL,
                stdout=_PIPE if want_stdout else _DEVNULL,
                stderr=(
                    _PIPE if want_stderr else _STDOUT if combined else _DEVNULL
                ),
                cwd=self.cwd,
                env=dict(self.env) if self.env is not None else None,
            )
        except OSError as exc:
            raise SubprocessStartError(str(exc)) from exc

        exit_code: int | None = None
        self.exit_code = None
        send, receive = anyio.create_memory_object_stream[LogEntry](100)
        handle = SubprocessLogHandle(receive)
        drained = False
        async with anyio.create_task_group() as task_group:
            try:
                try:
                    with send:
                        if want_stdout and process.stdout is not None:
                            task_group.start_soon(
                                self._pump,
                                process.stdout,
                                "combined" if combined else "stdout",
                                send.clone(),
                            )
                        if want_stderr and process.stderr is not None:
                            task_group.start_soon(
                                self._pump,
                                process.stderr,
                                "stderr",
                                send.clone(),
                            )
                    yield handle
                    drained = handle.drained
                    if drained:
                        exit_code = await process.wait()
                        self.exit_code = exit_code
                except BaseException:
                    task_group.cancel_scope.cancel()
                    raise
            finally:
                await self._cleanup(
                    process,
                    receive,
                    task_group,
                    exit_code=exit_code if drained else None,
                )

    async def _cleanup(
        self,
        process: Process,
        receive: MemoryObjectReceiveStream[LogEntry],
        task_group: TaskGroup,
        *,
        exit_code: int | None,
    ) -> None:
        """Reap the child and pumps; safe from any task (no shared scopes)."""
        receive.close()
        task_group.cancel_scope.cancel()
        # Locally created cancel scopes are entered and exited in the
        # currently executing task, so they are safe even when this runs
        # in the event loop's async-generator finalizer.  The scope is
        # shielded: cleanup typically runs while the consuming task is
        # being cancelled, and an unshielded await would re-raise the
        # cancellation before the child is killed, reaped, and its
        # streams are closed.
        with anyio.CancelScope(shield=True):
            await _close_process_streams(process)
            if exit_code is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                with anyio.move_on_after(self.cleanup_wait):
                    exit_code = await process.wait()
                    self.exit_code = exit_code
                if exit_code is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    with anyio.move_on_after(self.cleanup_wait):
                        exit_code = await process.wait()
                        self.exit_code = exit_code
            with contextlib.suppress(Exception):
                await process.aclose()
            await _close_asyncio_subprocess_transport(process)
            await checkpoint()

    async def _pump(
        self,
        stream: ByteReceiveStream,
        stream_name: Literal["stdout", "stderr", "combined"],
        send: MemoryObjectSendStream[LogEntry],
    ) -> None:
        buffer = b""
        with send:
            async for chunk in stream:
                lines, buffer = split_byte_lines(buffer, chunk)
                for line in lines:
                    await send.send(self._entry(line, stream_name))
            for line in flush_remainder(buffer):
                await send.send(self._entry(line, stream_name))

    def _entry(
        self,
        raw_line: bytes,
        stream_name: Literal["stdout", "stderr", "combined"],
    ) -> LogEntry:
        line = raw_line.decode(errors="replace")
        entry: LogEntry = {
            "message": line,
            "levelname": "INFO",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "lograil.stream": stream_name,
        }
        if self.name is not None:
            entry["name"] = self.name
            entry["lograil.process"] = self.name
        if self.subject is not None:
            entry["lograil.subject"] = self.subject
        if self.category is not None:
            entry["lograil.category"] = self.category
        if self.kind is not None:
            entry["lograil.kind"] = self.kind
        return entry


async def _close_process_streams(process: Process) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                await stream.aclose()


async def _close_asyncio_subprocess_transport(process: Process) -> None:
    raw_process = getattr(process, "_process", None)
    transport = getattr(raw_process, "_transport", None)
    if transport is None:
        return
    with contextlib.suppress(Exception):
        transport.close()
    await checkpoint()


@dataclass(slots=True)
class SubprocessLogHandle:
    """Async-iterable subprocess log stream returned by ``open()``."""

    _receive: MemoryObjectReceiveStream[LogEntry]
    drained: bool = False

    def __aiter__(self) -> SubprocessLogHandle:
        return self

    async def __anext__(self) -> LogEntry:
        try:
            return await self._receive.receive()
        except anyio.EndOfStream as exc:
            self.drained = True
            raise StopAsyncIteration from exc
