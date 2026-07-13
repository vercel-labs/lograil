# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from lograil import LogEntry
from lograil.sources.file import FileLogSource


def _run_source(
    source: FileLogSource,
    stop: threading.Event,
) -> tuple[list[LogEntry], threading.Thread]:
    entries: list[LogEntry] = []

    def target() -> None:
        with source.open(stop=stop) as handle:
            entries.extend(handle)

    thread = threading.Thread(target=target)
    thread.start()
    return entries, thread


def _wait_for_messages(entries: list[LogEntry], messages: list[str]) -> None:
    for _ in range(200):
        got = [str(entry["message"]) for entry in entries]
        if all(message in got for message in messages):
            return
        threading.Event().wait(0.01)
    msg = f"expected messages {messages!r}, got {entries!r}"
    raise AssertionError(msg)


def _append(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def _stop(stop: threading.Event, thread: threading.Thread) -> None:
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()


def _let_source_start() -> None:
    threading.Event().wait(0.05)


def test_existing_file_defaults_to_eof(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("old\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(FileLogSource(path, poll_interval=0.01), stop)
    try:
        _let_source_start()
        _append(path, "new\n")
        _wait_for_messages(entries, ["new"])
    finally:
        _stop(stop, thread)

    assert "old" not in [entry["message"] for entry in entries]


def test_existing_file_can_read_from_beginning(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("old\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, read_from="beginning", poll_interval=0.01), stop
    )
    try:
        _wait_for_messages(entries, ["old"])
    finally:
        _stop(stop, thread)


def test_file_source_emits_final_line_without_newline_on_stop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_text("first\nlast", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, read_from="beginning", poll_interval=0.01), stop
    )
    try:
        _wait_for_messages(entries, ["first"])
    finally:
        _stop(stop, thread)

    assert [entry["message"] for entry in entries] == ["first", "last"]


def test_file_source_closes_handles_when_context_exits(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_text("first\nsecond\n", encoding="utf-8")
    stop = threading.Event()
    source = FileLogSource(
        path,
        read_from="beginning",
        poll_interval=60,
    )

    with source.open(stop=stop) as entries:
        assert next(entries)["message"] == "first"
        generator_frame = entries.gi_frame
        assert generator_frame is not None
        open_files = generator_frame.f_locals["open_files"]
        state = next(iter(open_files.values()))
        handle = state.handle

    assert handle.closed


def test_missing_path_is_read_when_it_appears(tmp_path: Path) -> None:
    path = tmp_path / "later.log"
    stop = threading.Event()
    entries, thread = _run_source(FileLogSource(path, poll_interval=0.01), stop)
    try:
        path.write_text("hello\n", encoding="utf-8")
        _wait_for_messages(entries, ["hello"])
    finally:
        _stop(stop, thread)


def test_rotation_by_rename_reads_new_file_from_beginning(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("Windows cannot rename a file while it is being tailed")
    path = tmp_path / "app.log"
    path.write_text("old\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(FileLogSource(path, poll_interval=0.01), stop)
    try:
        _let_source_start()
        path.rename(tmp_path / "app.log.1")
        path.write_text("new\n", encoding="utf-8")
        _wait_for_messages(entries, ["new"])
    finally:
        _stop(stop, thread)


def test_truncation_resets_offset(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("old line\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(FileLogSource(path, poll_interval=0.01), stop)
    try:
        _let_source_start()
        path.write_text("new\n", encoding="utf-8")
        _wait_for_messages(entries, ["new"])
    finally:
        _stop(stop, thread)


def test_truncation_with_regrowth_past_old_offset_resets(
    tmp_path: Path,
) -> None:
    path = tmp_path / "app.log"
    path.write_text("old line one\nold line two\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(FileLogSource(path, poll_interval=0.01), stop)
    try:
        _let_source_start()
        # Rewrite in place (same inode) with content at least as large as
        # the old offset, as copytruncate plus a busy writer does between
        # two polls.
        path.write_text(
            "fresh line one\nfresh line two\nfresh line three\n",
            encoding="utf-8",
        )
        _wait_for_messages(
            entries,
            ["fresh line one", "fresh line two", "fresh line three"],
        )
    finally:
        _stop(stop, thread)


def test_glob_discovers_new_matching_files(tmp_path: Path) -> None:
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(tmp_path / "*.log", poll_interval=0.01), stop
    )
    try:
        _let_source_start()
        (tmp_path / "one.log").write_text("one\n", encoding="utf-8")
        _wait_for_messages(entries, ["one"])
    finally:
        _stop(stop, thread)


def test_relative_glob_names_are_absolute(tmp_path: Path) -> None:
    old_cwd = Path.cwd()
    path = tmp_path / "app.log"
    path.write_text("hello\n", encoding="utf-8")
    stop = threading.Event()
    try:
        os.chdir(tmp_path)
        entries, thread = _run_source(
            FileLogSource("*.log", read_from="beginning", poll_interval=0.01),
            stop,
        )
        try:
            _wait_for_messages(entries, ["hello"])
        finally:
            _stop(stop, thread)
    finally:
        os.chdir(old_cwd)

    assert entries[0]["name"] == str(path.resolve())


def test_chdir_after_init_does_not_reanchor_relative_path(
    tmp_path: Path,
) -> None:
    old_cwd = Path.cwd()
    anchor = tmp_path / "anchor"
    elsewhere = tmp_path / "elsewhere"
    anchor.mkdir()
    elsewhere.mkdir()
    (anchor / "app.log").write_text("anchored\n", encoding="utf-8")
    (elsewhere / "app.log").write_text("decoy\n", encoding="utf-8")
    stop = threading.Event()
    try:
        os.chdir(anchor)
        source = FileLogSource(
            "app.log", read_from="beginning", poll_interval=0.01
        )
        os.chdir(elsewhere)
        entries, thread = _run_source(source, stop)
        try:
            _wait_for_messages(entries, ["anchored"])
        finally:
            _stop(stop, thread)
    finally:
        os.chdir(old_cwd)

    messages = [entry["message"] for entry in entries]
    assert "decoy" not in messages
    assert entries[0]["name"] == str((anchor / "app.log").resolve())


def test_chdir_after_init_does_not_reanchor_relative_glob(
    tmp_path: Path,
) -> None:
    old_cwd = Path.cwd()
    anchor = tmp_path / "anchor"
    elsewhere = tmp_path / "elsewhere"
    anchor.mkdir()
    elsewhere.mkdir()
    (anchor / "app.log").write_text("anchored\n", encoding="utf-8")
    (elsewhere / "app.log").write_text("decoy\n", encoding="utf-8")
    stop = threading.Event()
    try:
        os.chdir(anchor)
        source = FileLogSource(
            "*.log", read_from="beginning", poll_interval=0.01
        )
        os.chdir(elsewhere)
        entries, thread = _run_source(source, stop)
        try:
            _wait_for_messages(entries, ["anchored"])
        finally:
            _stop(stop, thread)
    finally:
        os.chdir(old_cwd)

    messages = [entry["message"] for entry in entries]
    assert "decoy" not in messages
    assert entries[0]["name"] == str((anchor / "app.log").resolve())


def test_tail_lines_emits_backlog_then_follows(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, tail_lines=2, poll_interval=0.01), stop
    )
    try:
        _wait_for_messages(entries, ["four", "five"])
        _append(path, "six\n")
        _wait_for_messages(entries, ["six"])
    finally:
        _stop(stop, thread)

    messages = [entry["message"] for entry in entries]
    assert messages == ["four", "five", "six"]


def test_tail_lines_larger_than_file_reads_from_start(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("one\ntwo\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, tail_lines=10, poll_interval=0.01), stop
    )
    try:
        _wait_for_messages(entries, ["one", "two"])
    finally:
        _stop(stop, thread)

    assert [entry["message"] for entry in entries] == ["one", "two"]


def test_tail_lines_zero_behaves_like_plain_end(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    path.write_text("old\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, tail_lines=0, poll_interval=0.01), stop
    )
    try:
        _let_source_start()
        _append(path, "new\n")
        _wait_for_messages(entries, ["new"])
    finally:
        _stop(stop, thread)

    assert [entry["message"] for entry in entries] == ["new"]


def test_tail_lines_crosses_block_boundaries(tmp_path: Path) -> None:
    path = tmp_path / "app.log"
    lines = [f"line-{index:04d}" + "x" * 120 for index in range(200)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(path, tail_lines=150, poll_interval=0.01), stop
    )
    try:
        _wait_for_messages(entries, [lines[50], lines[199]])
    finally:
        _stop(stop, thread)

    assert [entry["message"] for entry in entries] == lines[50:]


def test_tail_lines_does_not_apply_to_files_discovered_later(
    tmp_path: Path,
) -> None:
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(tmp_path / "*.log", tail_lines=1, poll_interval=0.01),
        stop,
    )
    try:
        _let_source_start()
        (tmp_path / "new.log").write_text("a\nb\nc\n", encoding="utf-8")
        _wait_for_messages(entries, ["a", "b", "c"])
    finally:
        _stop(stop, thread)

    assert [entry["message"] for entry in entries] == ["a", "b", "c"]


def test_tail_lines_rejects_negative_values(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tail_lines"):
        FileLogSource(tmp_path / "app.log", tail_lines=-1)


def test_recursive_glob_discovers_nested_file(tmp_path: Path) -> None:
    nested = tmp_path / "logs" / "sub"
    nested.mkdir(parents=True)
    (nested / "app.log").write_text("hello\n", encoding="utf-8")
    stop = threading.Event()
    entries, thread = _run_source(
        FileLogSource(
            tmp_path / "logs" / "**" / "*.log",
            read_from="beginning",
            poll_interval=0.01,
        ),
        stop,
    )
    try:
        _wait_for_messages(entries, ["hello"])
    finally:
        _stop(stop, thread)
