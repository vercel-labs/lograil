# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Structured lifecycle stages and in-process progress."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing_extensions import Self

import contextlib
import logging
import time

from lograil._internal import log
from lograil._internal.progress import ProgressUpdate, StatusProgressRenderer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import TracebackType

_LOGGER = logging.getLogger("lograil.lifecycle")


def _entry(
    *,
    message: str,
    process: str,
    subject: str,
    stage_name: str,
    stage_status: str,
    level: int = logging.INFO,
) -> None:
    data = {
        "message": message,
        "levelname": logging.getLevelName(level),
        "name": _LOGGER.name,
        "lograil.process": process,
        "lograil.subject": subject,
        "lograil.stage": stage_name,
        "lograil.stage.status": stage_status,
    }
    # Use the currently configured lograil hierarchy so an active status
    # can consume lifecycle records even when the host configured a custom
    # logger name (for example, ``ggbuild``).
    log.tail_logger().log(level, message, extra={"lograil.entry": data})


@contextlib.contextmanager
def stage(
    name: str,
    *,
    process: str,
    subject: str,
    sticky: bool = False,
) -> Iterator[log.StatusHandle]:
    """Report one named lifecycle stage and manage its terminal status."""
    status_context: contextlib.AbstractContextManager[log.StatusHandle]
    if log.fancy_output_enabled():
        status_context = log.status(
            process=process,
            subject=subject,
            sticky=sticky,
            done=None,
        )
    else:
        status_context = contextlib.nullcontext(
            log.StatusHandle(_status=None, done=None)
        )
    with status_context as handle:
        _entry(
            message=f"{process} {subject}: started",
            process=process,
            subject=subject,
            stage_name=name,
            stage_status="started",
        )
        handle.restore()
        try:
            yield handle
        except BaseException:
            _entry(
                message=f"{process} {subject}: failed",
                process=process,
                subject=subject,
                stage_name=name,
                stage_status="failed",
                level=logging.ERROR,
            )
            raise
        else:
            _entry(
                message=f"{process} {subject}: finished",
                process=process,
                subject=subject,
                stage_name=name,
                stage_status="finished",
            )
            handle.restore()


class ProgressHandle:
    """Context-managed determinate or indeterminate progress."""

    def __init__(
        self,
        *,
        process: str,
        subject: str,
        description: str,
        total: int | None = None,
        interval: float = 0.1,
    ) -> None:
        if total is not None and total < 0:
            raise ValueError("progress total cannot be negative")
        self.process = process
        self.subject = subject
        self.description = description
        self.total = total
        self.completed = 0
        self.interval = interval
        self._renderer: StatusProgressRenderer | None = None
        self._last_emit = 0.0
        self._entered = False

    def __enter__(self) -> Self:
        if self._entered:
            raise RuntimeError("progress handle cannot be reused")
        self._entered = True
        self._renderer = StatusProgressRenderer(log.get_active_status())
        self._emit(force=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        _ = (exc, traceback)
        if exc_type is None and self.total is not None:
            self.completed = self.total
        self._emit(force=True)
        if self._renderer is not None:
            self._renderer.update(
                self._update(clear_label=True),
            )
            self._renderer.finish()

    def advance(self, amount: int = 1) -> None:
        """Advance progress by ``amount``."""
        self.update(completed=self.completed + amount)

    def update(
        self,
        *,
        completed: int | None = None,
        description: str | None = None,
        total: int | None = None,
    ) -> None:
        """Update progress values, subject to display rate limiting."""
        if completed is not None:
            self.completed = completed
        if description is not None:
            self.description = description
        if total is not None:
            if total < 0:
                raise ValueError("progress total cannot be negative")
            self.total = total
        self._emit(force=False)

    def _update(self, *, clear_label: bool = False) -> ProgressUpdate:
        return ProgressUpdate(
            description=self.description,
            completed=self.completed,
            total=self.total,
            process=self.process,
            subject=self.subject,
            clear_label=clear_label,
        )

    def _emit(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < self.interval:
            return
        self._last_emit = now
        update = self._update()
        if log.fancy_output_enabled():
            if self._renderer is not None:
                self._renderer.update(update)
            return
        data: dict[str, object] = {
            "message": self.description,
            "levelname": "INFO",
            "name": _LOGGER.name,
            "lograil.process": self.process,
            "lograil.subject": self.subject,
            "lograil.progress.description": self.description,
            "lograil.progress.completed": self.completed,
        }
        if self.total is not None:
            data["lograil.progress.total"] = self.total
        log.tail_logger().info(
            self.description,
            extra={"lograil.entry": data},
        )


def progress(
    *,
    process: str,
    subject: str,
    description: str,
    total: int | None = None,
) -> ProgressHandle:
    """Create a context-managed progress handle."""
    return ProgressHandle(
        process=process,
        subject=subject,
        description=description,
        total=total,
    )
