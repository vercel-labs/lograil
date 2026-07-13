# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Console utilities for lograil."""

from __future__ import annotations

from typing import IO, Any, cast

import sys

from rich.console import Console

_SYNC_START = "\x1b[?2026h"
_SYNC_END = "\x1b[?2026l"


def _real_stderr() -> IO[str] | None:
    """Return the underlying stderr stream.

    While a Live display is active rich redirects ``sys.stderr`` to a
    ``FileProxy`` that routes writes back through the console; writing
    there from the console's own output path would recurse, so unwrap it.
    """
    stderr = sys.stderr
    if stderr is None:
        return None
    proxied: IO[str] | None = getattr(stderr, "rich_proxied_file", None)
    return proxied if proxied is not None else stderr


class _SynchronizedStderr:
    """stderr proxy that brackets writes in DEC 2026 synchronized updates.

    Rich repaints a Live region by erasing every line and rewriting it,
    flushed as one ``write()`` per frame; a terminal that renders
    mid-frame shows the erased region as flicker.  Synchronized-update
    bracketing makes supporting terminals paint the frame atomically,
    and the codes are ignored elsewhere.  The target stream is resolved
    on every call so redirection and test capture keep working, and
    bracketing only applies when it is a terminal.
    """

    def write(self, text: str) -> int:
        stderr = _real_stderr()
        if stderr is None:
            return 0
        if text and stderr.isatty():
            stderr.write(f"{_SYNC_START}{text}{_SYNC_END}")
        else:
            stderr.write(text)
        return len(text)

    def flush(self) -> None:
        stderr = _real_stderr()
        if stderr is not None:
            stderr.flush()

    def __getattr__(self, name: str) -> Any:
        if name == "rich_proxied_file":
            # Console.file unwraps rich_proxied_file; exposing the one
            # from a redirected sys.stderr would let rich bypass this
            # proxy (and its bracketing) whenever a Live is active.
            raise AttributeError(name)
        return getattr(_real_stderr(), name)


stdout_console = Console()
stderr_console = Console(
    file=cast("IO[str]", _SynchronizedStderr()),
    stderr=True,
)
