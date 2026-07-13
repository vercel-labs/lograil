# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
r"""Shared newline framing helpers for byte and text streams.

Lines are terminated by ``\r\n``, ``\n``, or a lone ``\r`` -- and nothing
else.  Unicode line boundaries such as ``\f``, ``\x85``, ``\u2028`` are
message content, not line breaks.

A chunk that ends in ``\r`` is ambiguous: the ``\r`` may be the first half
of a ``\r\n`` pair split across a read boundary.  The splitters hold such a
trailing ``\r`` back in the returned remainder and resolve it against the
next chunk; at end of stream, :func:`flush_remainder` strips it and yields
the final partial line.
"""

from __future__ import annotations

from typing import overload

import re
from collections.abc import Iterator

__all__ = [
    "MAX_LINE_REMAINDER",
    "flush_remainder",
    "split_byte_lines",
    "split_text_lines",
]

MAX_LINE_REMAINDER = 1024 * 1024
"""Maximum unterminated line bytes/chars retained between stream chunks."""

# ``\r\n`` must be tried before lone ``\r``; a ``\r`` at the very end of the
# data (``\Z``) is not a terminator -- it is held back in the remainder.
_TEXT_TERMINATOR = re.compile(r"\r\n|\n|\r(?!\Z)")
_BYTE_TERMINATOR = re.compile(rb"\r\n|\n|\r(?!\Z)")


def split_text_lines(buffer: str, chunk: str) -> tuple[list[str], str]:
    r"""Append ``chunk`` to ``buffer`` and return complete text lines.

    Returns ``(lines, remainder)`` where ``lines`` are complete lines with
    their terminators stripped and ``remainder`` is the unterminated tail
    (possibly ending in a held-back ``\r``) to pass to the next call.
    """
    return _split_str_lines(buffer, chunk, MAX_LINE_REMAINDER)


def split_byte_lines(buffer: bytes, chunk: bytes) -> tuple[list[bytes], bytes]:
    r"""Append ``chunk`` to ``buffer`` and return complete byte lines.

    Returns ``(lines, remainder)`` where ``lines`` are complete lines with
    their terminators stripped and ``remainder`` is the unterminated tail
    (possibly ending in a held-back ``\r``) to pass to the next call.
    """
    return _split_bytes_lines(buffer, chunk, MAX_LINE_REMAINDER)


@overload
def flush_remainder(remainder: str) -> Iterator[str]: ...


@overload
def flush_remainder(remainder: bytes) -> Iterator[bytes]: ...


def flush_remainder(remainder: str | bytes) -> Iterator[str] | Iterator[bytes]:
    r"""Yield the final partial line buffered in ``remainder``, if any.

    Strips at most one trailing terminator (a held-back ``\r``); yields
    nothing when the remainder is empty or holds only a terminator.
    """
    if isinstance(remainder, bytes):
        return _flush_bytes(remainder)
    return _flush_text(remainder)


def _flush_text(remainder: str) -> Iterator[str]:
    if remainder.endswith("\r\n"):
        remainder = remainder[:-2]
    elif remainder.endswith(("\r", "\n")):
        remainder = remainder[:-1]
    if remainder:
        yield remainder


def _flush_bytes(remainder: bytes) -> Iterator[bytes]:
    if remainder.endswith(b"\r\n"):
        remainder = remainder[:-2]
    elif remainder.endswith((b"\r", b"\n")):
        remainder = remainder[:-1]
    if remainder:
        yield remainder


def _split_str_lines(
    buffer: str,
    chunk: str,
    max_remainder: int,
) -> tuple[list[str], str]:
    lines = _TEXT_TERMINATOR.split(buffer + chunk)
    remainder = lines.pop()
    while len(remainder) > max_remainder:
        lines.append(remainder[:max_remainder])
        remainder = remainder[max_remainder:]
    return lines, remainder


def _split_bytes_lines(
    buffer: bytes,
    chunk: bytes,
    max_remainder: int,
) -> tuple[list[bytes], bytes]:
    lines = _BYTE_TERMINATOR.split(buffer + chunk)
    remainder = lines.pop()
    while len(remainder) > max_remainder:
        lines.append(remainder[:max_remainder])
        remainder = remainder[max_remainder:]
    return lines, remainder
