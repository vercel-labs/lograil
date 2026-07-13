# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import anyio
import pytest

from lograil import SubprocessLogSource

if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259
    _INHERIT_HANDLE = 0

    def _process_exists(pid: int) -> bool:
        handle = ctypes.windll.kernel32.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            _INHERIT_HANDLE,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            )
            return bool(ok) and exit_code.value == _STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

else:

    def _process_exists(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


async def _collect(source: SubprocessLogSource) -> list[dict[str, object]]:
    async with source.open() as entries:
        return [entry async for entry in entries]


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _kill_process(pid: int) -> None:
    if sys.platform == "win32":
        os.kill(pid, 9)
        return
    os.kill(pid, 9)


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_subprocess_source_defaults_to_stderr(backend: str) -> None:
    source = SubprocessLogSource(
        _python("import sys; print('out'); print('err', file=sys.stderr)")
    )

    entries = anyio.run(_collect, source, backend=backend)

    assert [entry["message"] for entry in entries] == ["err"]
    assert entries[0]["lograil.stream"] == "stderr"
    assert entries[0]["levelname"] == "INFO"
    assert source.exit_code == 0


def test_subprocess_source_stdout_mode_captures_stdout_only() -> None:
    source = SubprocessLogSource(
        _python("import sys; print('out'); print('err', file=sys.stderr)"),
        stream="stdout",
    )

    entries = anyio.run(_collect, source)

    assert [entry["message"] for entry in entries] == ["out"]
    assert entries[0]["lograil.stream"] == "stdout"


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_subprocess_source_combined_mode_preserves_write_order(
    backend: str,
) -> None:
    source = SubprocessLogSource(
        _python(
            "import sys; "
            "print('one'); sys.stdout.flush(); "
            "print('two', file=sys.stderr); sys.stderr.flush(); "
            "print('three'); sys.stdout.flush()"
        ),
        stream="combined",
    )

    entries = anyio.run(_collect, source, backend=backend)

    # stderr is merged into stdout at the fd level (like 2>&1), so the
    # child's relative write order is preserved; per-stream attribution
    # is lost in the merge.
    assert [entry["message"] for entry in entries] == ["one", "two", "three"]
    assert {entry["lograil.stream"] for entry in entries} == {"combined"}


def test_subprocess_source_preserves_nonzero_exit_code() -> None:
    source = SubprocessLogSource(_python("raise SystemExit(7)"))

    anyio.run(_collect, source)

    assert source.exit_code == 7


def test_subprocess_source_passes_cwd_and_env(tmp_path: Path) -> None:
    path = os.fspath(tmp_path)
    source = SubprocessLogSource(
        _python(
            "import os, sys; "
            "print(os.getcwd()); "
            "print(os.environ['LOGRAIL_TEST_ENV'], file=sys.stderr)"
        ),
        cwd=path,
        env={**os.environ, "LOGRAIL_TEST_ENV": "env-ok"},
        stream="combined",
    )

    entries = anyio.run(_collect, source)

    assert {entry["message"] for entry in entries} == {path, "env-ok"}


async def _read_first_after_delay(source: SubprocessLogSource) -> float:
    started = time.monotonic()
    async with source.open() as entries:
        async for entry in entries:
            assert entry["message"] == "ready"
            return time.monotonic() - started
    raise AssertionError("source produced no entries")


def test_subprocess_source_streams_before_process_exit() -> None:
    source = SubprocessLogSource(
        _python(
            "import sys, time; print('ready', file=sys.stderr); "
            "sys.stderr.flush(); time.sleep(1.0)"
        )
    )

    elapsed = anyio.run(_read_first_after_delay, source)

    assert elapsed < 0.5


async def _read_one_and_break(source: SubprocessLogSource) -> str:
    async with source.open() as entries:
        async for entry in entries:
            return str(entry["message"])
    raise AssertionError("source produced no entries")


@pytest.mark.parametrize("backend", ["asyncio", "trio"])
def test_subprocess_source_early_break_terminates_child(
    backend: str,
) -> None:
    source = SubprocessLogSource(
        _python(
            "import os, sys, time; print(os.getpid(), file=sys.stderr); "
            "sys.stderr.flush(); time.sleep(30)"
        )
    )

    start = time.monotonic()
    pid = int(anyio.run(_read_one_and_break, source, backend=backend))
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, "early break must not hang in generator cleanup"
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            break
        time.sleep(0.05)
    else:
        _kill_process(pid)
        raise AssertionError("child leaked after early break")


def test_subprocess_source_early_break_kills_term_trapping_child() -> None:
    source = SubprocessLogSource(
        [
            "sh",
            "-c",
            'trap "" TERM; echo ready >&2; sleep 30',
        ],
        cleanup_wait=0.05,
    )

    start = time.monotonic()
    message = anyio.run(_read_one_and_break, source)
    elapsed = time.monotonic() - start

    assert message == "ready"
    assert elapsed < 1.0, "kill escalation must bound cleanup time"
