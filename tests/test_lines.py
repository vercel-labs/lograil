# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from lograil._internal.lines import (
    MAX_LINE_REMAINDER,
    flush_remainder,
    split_byte_lines,
    split_text_lines,
)


def test_byte_lines_split_on_lf() -> None:
    assert split_byte_lines(b"", b"one\ntwo\n") == ([b"one", b"two"], b"")


def test_text_lines_split_on_lf() -> None:
    assert split_text_lines("", "one\ntwo\n") == (["one", "two"], "")


def test_partial_line_is_kept_in_remainder() -> None:
    assert split_byte_lines(b"", b"one\npart") == ([b"one"], b"part")
    assert split_text_lines("", "one\npart") == (["one"], "part")


def test_crlf_is_a_single_terminator() -> None:
    assert split_byte_lines(b"", b"one\r\ntwo\r\n") == ([b"one", b"two"], b"")
    assert split_text_lines("", "one\r\ntwo\r\n") == (["one", "two"], "")


def test_lone_carriage_return_terminates_a_line() -> None:
    assert split_byte_lines(b"", b"step 1\rstep 2\n") == (
        [b"step 1", b"step 2"],
        b"",
    )
    assert split_text_lines("", "step 1\rstep 2\n") == (
        ["step 1", "step 2"],
        "",
    )


def test_chunk_final_cr_is_held_back() -> None:
    lines, remainder = split_byte_lines(b"", b"hello\r")
    assert lines == []
    assert remainder == b"hello\r"


def test_crlf_split_across_chunks_yields_no_phantom_empty_line() -> None:
    lines, remainder = split_byte_lines(b"", b"hello\r")
    more, remainder = split_byte_lines(remainder, b"\nworld\n")
    assert lines + more == [b"hello", b"world"]
    assert remainder == b""


def test_text_crlf_split_across_chunks_yields_no_phantom_empty_line() -> None:
    lines, remainder = split_text_lines("", "hello\r")
    more, remainder = split_text_lines(remainder, "\nworld\n")
    assert lines + more == ["hello", "world"]
    assert not remainder


def test_held_cr_resolves_as_lone_terminator_against_next_chunk() -> None:
    lines, remainder = split_byte_lines(b"", b"hello\r")
    more, remainder = split_byte_lines(remainder, b"world\n")
    assert lines + more == [b"hello", b"world"]
    assert remainder == b""


def test_consecutive_cr_terminators_across_chunks() -> None:
    lines, remainder = split_byte_lines(b"", b"a\r\r")
    assert lines == [b"a"]
    assert remainder == b"\r"
    more, remainder = split_byte_lines(remainder, b"b\n")
    assert more == [b"", b"b"]
    assert remainder == b""


@pytest.mark.parametrize(
    "control",
    ["\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_unicode_line_boundaries_do_not_split_text(control: str) -> None:
    line = f"a{control}b"
    assert split_text_lines("", line + "\n") == ([line], "")


def test_form_feed_does_not_split_bytes() -> None:
    assert split_byte_lines(b"", b"a\x0cb\n") == ([b"a\x0cb"], b"")


def test_text_and_bytes_variants_agree() -> None:
    data = "a\x0cb\r\nc\rd\ne"
    text_lines, text_rem = split_text_lines("", data)
    byte_lines, byte_rem = split_byte_lines(b"", data.encode())
    assert [line.encode() for line in text_lines] == byte_lines
    assert text_rem.encode() == byte_rem


def test_empty_lines_are_preserved() -> None:
    assert split_byte_lines(b"", b"a\n\nb\n") == ([b"a", b"", b"b"], b"")


def test_flush_remainder_yields_partial_line() -> None:
    assert list(flush_remainder(b"partial")) == [b"partial"]
    assert list(flush_remainder("partial")) == ["partial"]


def test_flush_remainder_strips_held_cr() -> None:
    assert list(flush_remainder(b"hello\r")) == [b"hello"]
    assert list(flush_remainder("hello\r")) == ["hello"]


def test_flush_remainder_yields_nothing_when_empty() -> None:
    assert list(flush_remainder(b"")) == []
    assert list(flush_remainder("")) == []


def test_flush_remainder_yields_nothing_for_bare_terminator() -> None:
    assert list(flush_remainder(b"\r")) == []
    assert list(flush_remainder("\r")) == []


def test_flush_remainder_strips_at_most_one_terminator() -> None:
    assert list(flush_remainder("x\r\n")) == ["x"]
    assert list(flush_remainder(b"x\n")) == [b"x"]


def test_newline_free_remainder_is_bounded() -> None:
    lines, remainder = split_byte_lines(
        b"",
        b"x" * (MAX_LINE_REMAINDER + 3),
    )

    assert lines == [b"x" * MAX_LINE_REMAINDER]
    assert remainder == b"xxx"
