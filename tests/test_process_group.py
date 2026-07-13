# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any, ClassVar
from typing_extensions import Self

import io
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from lograil import ProcessSpec, configure_logging, run_process_group
from lograil._internal import progress, remap
from lograil._internal.process import (
    _CANCELLED_EXIT_CODE,
    _parser_binding_for,
    _parser_for,
    _ProcessDashboard,
    _ProcessState,
    _record_entry,
)
from lograil.parsers import OutputParserCapabilities, register_output_parser
from lograil.parsers._base import registered_output_parsers


@pytest.fixture(autouse=True)
def quiet_process_output() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch("lograil._internal.console.stderr_console.print")
        )
        stack.enter_context(
            patch("lograil._internal.log.sys.stderr", io.StringIO())
        )
        yield


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _print_stderr(message: str, *, delay: float | None = None) -> str:
    sleep = f"time.sleep({delay}); " if delay is not None else ""
    return f"import time, sys; {sleep}print({message!r}, file=sys.stderr)"


class _ProgressParser:
    capabilities = OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    )

    def __call__(self, entry: dict[str, Any]) -> dict[str, Any]:
        entry["lograil.status.detail"] = str(entry.get("message", ""))
        return entry


class _RecordingLive:
    frames: ClassVar[list[str]] = []
    refreshes: ClassVar[list[bool]] = []

    def __init__(self, renderable: object, **kwargs: Any) -> None:
        _ = kwargs
        self._record(renderable)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        _ = exc

    def update(self, renderable: object, *, refresh: bool = False) -> None:
        self.refreshes.append(refresh)
        self._record(renderable)

    @classmethod
    def _record(cls, renderable: object) -> None:
        console = Console(record=True, width=100, file=io.StringIO())
        console.print(renderable)
        cls.frames.append(console.export_text())


def test_process_group_runs_multiple_processes_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    first_ready = tmp_path / "first.ready"
    second_ready = tmp_path / "second.ready"

    def wait_for_peer(message: str, own: Path, peer: Path) -> list[str]:
        code = (
            "import sys, time; "
            "from pathlib import Path; "
            "message, own, peer = sys.argv[1:4]; "
            "Path(own).write_text('ready', encoding='utf-8'); "
            "deadline = time.monotonic() + 1.0; "
            "\nwhile not Path(peer).exists():"
            "\n    if time.monotonic() >= deadline:"
            "\n        raise SystemExit(9)"
            "\n    time.sleep(0.01)"
            "\nprint(message, file=sys.stderr)"
        )
        return [*_python(code), message, str(own), str(peer)]

    specs = [
        ProcessSpec(
            wait_for_peer("one", first_ready, second_ready),
            name="one",
            category="typeck",
        ),
        ProcessSpec(
            wait_for_peer("two", second_ready, first_ready),
            name="two",
            category="lint",
        ),
    ]

    result = run_process_group(specs)

    assert result.success is True
    assert {item.last_message for item in result.processes} == {"one", "two"}


def test_process_group_completes_all_processes_when_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    specs = [
        ProcessSpec(
            _python(_print_stderr("bad") + "; raise SystemExit(3)"),
            name="bad",
        ),
        ProcessSpec(_python(_print_stderr("good")), name="good"),
    ]

    result = run_process_group(specs)

    assert result.success is False
    by_name = {item.spec.label: item for item in result.processes}
    assert by_name["bad"].exit_code == 3
    assert by_name["bad"].success is False
    assert by_name["good"].exit_code == 0
    assert by_name["good"].success is True


def test_process_group_finishes_cancelled_sibling_in_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    _RecordingLive.frames = []
    _RecordingLive.refreshes = []
    specs = [
        ProcessSpec(
            _python(_print_stderr("bad") + "; raise SystemExit(3)"),
            name="bad",
        ),
        ProcessSpec(
            _python(
                "import time, sys; "
                "print('started', file=sys.stderr, flush=True); "
                "time.sleep(30)"
            ),
            name="long",
        ),
    ]

    with patch("lograil._internal.process.Live", _RecordingLive):
        result = run_process_group(specs, cancel_on_failure=True)

    by_name = {item.spec.label: item for item in result.processes}
    assert by_name["bad"].exit_code == 3
    cancelled_codes = {1} if os.name == "nt" else {-15, _CANCELLED_EXIT_CODE}
    assert by_name["long"].exit_code in cancelled_codes
    assert by_name["long"].success is False
    assert by_name["long"].last_message == "cancelled"
    assert _RecordingLive.frames
    final_frame = _RecordingLive.frames[-1]
    assert "long cancelled" in final_frame
    assert "✗" in final_frame
    assert "⠋" not in final_frame
    assert True in _RecordingLive.refreshes


def test_process_group_batches_live_refreshes_for_progress_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    _RecordingLive.frames = []
    _RecordingLive.refreshes = []
    progress_lines = [
        progress.format_line(description="test", completed=index, total=3)
        for index in range(1, 4)
    ]
    code = "; ".join(f"print({line!r})" for line in progress_lines)

    with patch("lograil._internal.process.Live", _RecordingLive):
        result = run_process_group([
            ProcessSpec(
                _python(code),
                name="worker",
                env={**os.environ, **progress.lograil_instrumentation_env()},
            )
        ])

    assert result.success is True
    assert False in _RecordingLive.refreshes
    assert True in _RecordingLive.refreshes


def test_process_group_records_spawn_failure_without_cancelling_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    specs = [
        ProcessSpec(["definitely-not-a-lograil-test-binary"], name="missing"),
        ProcessSpec(_python(_print_stderr("good")), name="good"),
    ]

    result = run_process_group(specs)

    assert result.success is False
    by_name = {item.spec.label: item for item in result.processes}
    assert by_name["missing"].success is False
    assert by_name["missing"].last_message is not None
    assert "failed to start" in by_name["missing"].last_message
    assert by_name["good"].success is True


def test_process_group_routes_output_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    spec = ProcessSpec(
        _python("import sys; print('hello', file=sys.stderr)"),
        name="ruff",
        subject="pkg-a",
        category="lint",
    )

    result = run_process_group([spec])

    entry = result.processes[0].tail[-1]
    assert entry["name"] == "ruff"
    assert entry["lograil.process"] == "ruff"
    assert entry["lograil.subject"] == "pkg-a"
    assert entry["lograil.category"] == "lint"


def test_process_group_default_stream_captures_instrumented_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    env = {**os.environ, **progress.lograil_instrumentation_env()}
    progress_line = progress.format_line(
        description="compile",
        completed=1,
        total=2,
    )

    result = run_process_group([
        ProcessSpec(
            _python(f"print({progress_line!r})"),
            name="worker",
            env=env,
        )
    ])

    process = result.processes[0]
    assert process.success is True
    assert any(
        entry.get(remap.PROGRESS_DESCRIPTION) == "compile"
        for entry in process.tail
    )


def test_process_group_plain_output_respects_lograil_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    monkeypatch.setenv("LOGRAIL", "off")
    configure_logging()

    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        result = run_process_group([
            ProcessSpec(_python(_print_stderr("hidden")), name="worker")
        ])

    assert result.success is True
    assert result.processes[0].last_message == "hidden"
    assert mock_print.call_count == 0


def test_process_group_escapes_bracketed_process_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    result = run_process_group([
        ProcessSpec(_python(_print_stderr("ok")), name="scan[/tmp]")
    ])

    assert result.success is True


def test_plain_and_json_modes_do_not_create_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for mode in ("plain", "json"):
        monkeypatch.setenv("LOGRAIL_OUTPUT", mode)
        configure_logging()
        with patch("lograil._internal.process.Live") as live:
            run_process_group([ProcessSpec(_python(_print_stderr("done")))])
        assert live.call_count == 0


def test_dashboard_renders_grouped_spinner_states() -> None:
    running = _ProcessState(
        ProcessSpec(["ruff"], name="ruff", category="lint"), detail="checking"
    )
    ok = _ProcessState(ProcessSpec(["mypy"], name="mypy", category="typeck"))
    ok.running = False
    ok.exit_code = 0
    failed = _ProcessState(ProcessSpec(["ty"], name="ty", category="typeck"))
    failed.running = False
    failed.exit_code = 1
    dashboard = _ProcessDashboard([running, ok, failed])
    capture_console = Console(record=True, width=100, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()

    assert "ruff" in rendered
    assert "mypy" in rendered
    assert "ty" in rendered
    assert "✓" in rendered
    assert "✗" in rendered


def test_dashboard_renders_pytest_as_progress_from_start() -> None:
    pytest_state = _ProcessState(
        ProcessSpec(
            ["pytest"],
            name="root",
            category="test",
        )
    )
    quick_state = _ProcessState(
        ProcessSpec(["python", "-c", "pass"], name="vercel", category="test")
    )
    quick_state.running = False
    quick_state.exit_code = 0
    dashboard = _ProcessDashboard([quick_state, pytest_state])
    capture_console = Console(record=True, width=100, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()

    assert "✓ vercel" in rendered
    assert "root" in rendered
    assert "0%" in rendered
    assert "starting pytest" in rendered
    assert not rendered.startswith("test ")


def test_dashboard_pytest_detail_does_not_wrap() -> None:
    state = _ProcessState(
        ProcessSpec(
            ["pytest"],
            name="root",
            category="test",
        ),
        detail="tests/unit/test_pty_session.py::test_run_interactive_loop_uses_session_surface",
    )
    state.total = 100
    state.completed = 25
    dashboard = _ProcessDashboard([state])
    capture_console = Console(record=True, width=72, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()

    assert rendered.count("\n") == 1
    assert "tests/unit/test_pty_session.py" in rendered


def test_dashboard_marks_successful_pytest_without_output_complete() -> None:
    state = _ProcessState(
        ProcessSpec(
            ["pytest"],
            name="vercel",
            category="test",
        )
    )
    dashboard = _ProcessDashboard([state])

    dashboard.finish(state, 0)

    assert state.completed == 1
    assert state.total == 1


def test_dashboard_shows_section_once_when_multiple_sections_render() -> None:
    lint = _ProcessState(ProcessSpec(["ruff"], name="ruff", category="lint"))
    test_a = _ProcessState(
        ProcessSpec(["pytest"], name="root", category="test")
    )
    test_b = _ProcessState(
        ProcessSpec(["pytest"], name="queue", category="test")
    )
    dashboard = _ProcessDashboard([lint, test_a, test_b])
    capture_console = Console(record=True, width=100, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()
    lines = rendered.splitlines()

    assert rendered.count("lint") == 1
    assert sum(line.startswith("test ") for line in lines) == 1


def test_dashboard_keeps_section_label_when_progress_detail_is_long() -> None:
    lint = _ProcessState(ProcessSpec(["ruff"], name="ruff", category="lint"))
    test = _ProcessState(
        ProcessSpec(["pytest"], name="root", category="test"),
        detail="tests/unit/test_pty_session.py::test_run_interactive_loop_uses_session_surface",
    )
    test.total = 100
    test.completed = 53
    dashboard = _ProcessDashboard([lint, test])
    capture_console = Console(record=True, width=92, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()

    assert "test" in rendered
    assert "test…" not in rendered
    assert " 53%" in rendered


def test_dashboard_hides_progress_detail_when_complete() -> None:
    state = _ProcessState(
        ProcessSpec(["pytest"], name="root", category="test"),
        detail="tests/unit/test_pty_session.py::test_run_interactive_loop_uses_session_surface",
    )
    state.running = False
    state.exit_code = 0
    state.total = 100
    state.completed = 100
    dashboard = _ProcessDashboard([state])
    capture_console = Console(record=True, width=100, file=io.StringIO())

    capture_console.print(dashboard)
    rendered = capture_console.export_text()

    assert "100%" in rendered
    assert "test_pty_session" not in rendered


def test_pytest_parser_updates_progress_and_current_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()
    code = (
        "import sys; "
        "print('collected 2 items', file=sys.stderr); "
        "print('tests/test_a.py::test_one PASSED [ 50%]', file=sys.stderr); "
        "print('tests/test_a.py::test_two FAILED [100%]', file=sys.stderr); "
    )

    result = run_process_group([
        ProcessSpec(
            _python(code),
            name="pytest",
            kind="pytest",
        )
    ])

    process = result.processes[0]
    assert process.success is True
    assert process.last_message == "tests/test_a.py::test_two"
    assert any(
        entry.get("lograil.progress.total") == 2 for entry in process.tail
    )


def test_pytest_startup_detail_forces_live_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    _RecordingLive.frames = []
    _RecordingLive.refreshes = []
    code = (
        "import sys; "
        "print('created: 14/14 workers', file=sys.stderr); "
        "print('14 workers [2 items]', file=sys.stderr); "
        "print('scheduling tests via LoadScheduling', file=sys.stderr); "
    )

    with patch("lograil._internal.process.Live", _RecordingLive):
        result = run_process_group([
            ProcessSpec(
                _python(code),
                name="pytest",
                kind="pytest",
            )
        ])

    assert result.success is True
    rendered = "\n".join(_RecordingLive.frames)
    assert "created: 14/14 workers" in rendered
    assert "14 workers [2 items]" in rendered
    assert "scheduling tests via LoadScheduling" in rendered
    assert True in _RecordingLive.refreshes


def test_pytest_unknown_total_keeps_spinner_only_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    result = run_process_group([
        ProcessSpec(
            _python(
                "import sys; "
                "print('tests/test_a.py::test_one PASSED', file=sys.stderr)"
            ),
            name="pytest",
            kind="pytest",
        )
    ])

    process = result.processes[0]
    assert process.last_message == "tests/test_a.py::test_one"
    assert not any("lograil.progress.total" in entry for entry in process.tail)


def test_plain_process_group_interprets_child_ansi_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "plain")
    configure_logging()

    with patch("lograil._internal.console.stderr_console.print") as mock_print:
        result = run_process_group([
            ProcessSpec(
                _python(
                    "import sys; "
                    "print('\\x1b[36m[ 50%]\\x1b[0m\\x1b[32mPASSED\\x1b[0m "
                    "tests/test_a.py::test_one', file=sys.stderr)"
                ),
                name="pytest",
                kind="pytest",
            )
        ])

    assert result.success is True
    rendered = mock_print.call_args.args[0]
    assert rendered.plain.endswith("[ 50%]PASSED tests/test_a.py::test_one")
    assert rendered.spans


def test_pytest_parser_is_auto_selected_for_any_pytest_command() -> None:
    for argv in (["pytest"], ["pytest", "-q"], ["pytest", "tests", "-x"]):
        binding = _parser_binding_for(ProcessSpec(argv))

        assert binding.name == "pytest"


def test_pytest_parser_detection_uses_command_basename() -> None:
    binding = _parser_binding_for(ProcessSpec(["/venv/bin/pytest", "-q"]))

    assert binding.name == "pytest"


def test_kind_can_auto_select_parser_when_command_name_differs() -> None:
    binding = _parser_binding_for(
        ProcessSpec([sys.executable, "-m", "pytest"], kind="pytest")
    )

    assert binding.name == "pytest"


def test_explicit_parser_overrides_auto_detection() -> None:
    parser = _parser_for(ProcessSpec(["pytest"], parser="generic"))
    entry = parser({"message": "tests/test_a.py::test_one PASSED [100%]"})

    assert entry["lograil.status.detail"] == (
        "tests/test_a.py::test_one PASSED [100%]"
    )
    assert "lograil.progress.total" not in entry


def test_custom_registered_parser_command_detection() -> None:
    register_output_parser(
        "lograil-test-command-parser",
        _ProgressParser,
        command_names=("custom-test",),
        capabilities=_ProgressParser.capabilities,
        replace=True,
    )

    binding = _parser_binding_for(ProcessSpec(["/tmp/custom-test"]))

    assert binding.name == "lograil-test-command-parser"
    assert binding.capabilities.starts_progress is True


def test_custom_registered_parser_predicate_detection() -> None:
    register_output_parser(
        "lograil-test-predicate-parser",
        _ProgressParser,
        predicate=lambda spec: spec.name == "predicate-target",
        replace=True,
    )

    binding = _parser_binding_for(
        ProcessSpec(["python"], name="predicate-target")
    )

    assert binding.name == "lograil-test-predicate-parser"


def test_parser_detection_priority_and_order_are_deterministic() -> None:
    register_output_parser(
        "lograil-test-lower-priority-parser",
        _ProgressParser,
        command_names=("same-command",),
        priority=0,
        replace=True,
    )
    register_output_parser(
        "lograil-test-higher-priority-parser",
        _ProgressParser,
        command_names=("same-command",),
        priority=10,
        replace=True,
    )
    register_output_parser(
        "lograil-test-first-tie-parser",
        _ProgressParser,
        command_names=("tie-command",),
        priority=5,
        replace=True,
    )
    register_output_parser(
        "lograil-test-second-tie-parser",
        _ProgressParser,
        command_names=("tie-command",),
        priority=5,
        replace=True,
    )

    assert _parser_binding_for(ProcessSpec(["same-command"])).name == (
        "lograil-test-higher-priority-parser"
    )
    assert _parser_binding_for(ProcessSpec(["tie-command"])).name == (
        "lograil-test-first-tie-parser"
    )


def test_unknown_explicit_parser_name_fails_clearly() -> None:
    with pytest.raises(ValueError, match="unknown output parser 'missing'"):
        _parser_for(ProcessSpec(["python"], parser="missing"))


def test_direct_callable_parser_and_capabilities_work() -> None:
    state = _ProcessState(ProcessSpec(["python"], parser=_ProgressParser()))
    dashboard = _ProcessDashboard([state])

    dashboard.finish(state, 0)

    assert state.parser.name is None
    assert state.total == 1
    assert state.completed == 1


def test_builtin_output_parsers_are_registered() -> None:
    assert "generic" in registered_output_parsers()
    assert "pytest" in registered_output_parsers()


def _pytest_spec() -> ProcessSpec:
    return ProcessSpec(["pytest"], name="pytest")


def test_pytest_parser_emits_generic_progress_keys() -> None:
    parser = _parser_for(_pytest_spec())

    collected = parser({"message": "collected 3 items"})
    assert collected["lograil.status.detail"] == "collected 3 items"
    assert collected["lograil.progress.description"] == "pytest"
    assert collected["lograil.progress.completed"] == 0
    assert collected["lograil.progress.total"] == 3
    assert "lograil.pytest.percent" not in collected
    assert "lograil.pytest.total" not in collected

    running = parser({"message": "tests/test_a.py::test_two PASSED [ 67%]"})
    assert running["lograil.progress.description"] == "pytest"
    assert running["lograil.progress.completed"] == 2
    assert running["lograil.progress.total"] == 3
    assert "lograil.pytest.percent" not in running

    done = parser({"message": "tests/test_a.py::test_three PASSED [100%]"})
    assert done["lograil.progress.completed"] == 3
    assert done["lograil.progress.total"] == 3


def test_pytest_parser_surfaces_collection_and_worker_startup() -> None:
    parser = _parser_for(_pytest_spec())

    collecting = parser({"message": "collecting ..."})
    created = parser({"message": "created: 14/14 workers"})
    workers = parser({"message": "14 workers [32 items]"})
    scheduling = parser({"message": "scheduling tests via LoadScheduling"})

    assert collecting["lograil.status.detail"] == "collecting ..."
    assert created["lograil.status.detail"] == "created: 14/14 workers"
    assert workers["lograil.status.detail"] == "14 workers [32 items]"
    assert workers["lograil.progress.completed"] == 0
    assert workers["lograil.progress.total"] == 32
    assert (
        scheduling["lograil.status.detail"]
        == "scheduling tests via LoadScheduling"
    )


def test_pytest_parser_counts_compact_xdist_progress() -> None:
    parser = _parser_for(_pytest_spec())

    workers = parser({"message": "14 workers [6 items]"})
    progress = parser({"message": "......"})

    assert workers["lograil.progress.total"] == 6
    assert progress["lograil.status.detail"] == "pytest"
    assert progress["lograil.progress.completed"] == 6
    assert progress["lograil.progress.total"] == 6


def test_pytest_parser_percent_without_total_uses_percent_scale() -> None:
    parser = _parser_for(_pytest_spec())

    entry = parser({"message": "tests/test_a.py::test_one PASSED [ 40%]"})

    assert entry["lograil.progress.completed"] == 40
    assert entry["lograil.progress.total"] == 100


def test_pytest_parser_advances_for_scheduled_nodeids() -> None:
    parser = _parser_for(_pytest_spec())
    parser({"message": "14 workers [4 items]"})

    first = parser({"message": "tests/test_a.py::test_one"})
    second = parser({"message": "tests/test_a.py::test_two"})
    repeated = parser({"message": "tests/test_a.py::test_two"})
    completion = parser({
        "message": "[gw0] [ 25%] PASSED tests/test_a.py::test_one"
    })

    assert first["lograil.progress.completed"] == 1
    assert first["lograil.progress.total"] == 4
    assert second["lograil.progress.completed"] == 2
    assert repeated["lograil.progress.completed"] == 2
    assert completion["lograil.progress.completed"] == 2


def test_pytest_parser_state_is_per_process() -> None:
    first = _parser_for(_pytest_spec())
    second = _parser_for(_pytest_spec())
    first({"message": "collected 4 items"})

    entry = second({"message": "tests/test_a.py::test_one PASSED [ 50%]"})

    assert entry["lograil.progress.total"] == 100
    assert entry["lograil.progress.completed"] == 50


def test_record_entry_ignores_legacy_pytest_percent_key() -> None:
    state = _ProcessState(_pytest_spec())
    state.total = 4

    _record_entry(state, {"message": "x", "lograil.pytest.percent": 50})

    assert state.completed is None
    assert state.total == 4


def test_pytest_dashboard_percent_matches_pytest_output() -> None:
    parser = _parser_for(_pytest_spec())
    state = _ProcessState(_pytest_spec())
    lines = [
        "collected 3 items",
        "tests/test_a.py::test_one PASSED [ 33%]",
        "tests/test_a.py::test_two PASSED [ 67%]",
        "tests/test_a.py::test_three PASSED [100%]",
    ]
    shown: list[int] = []
    for line in lines:
        entry = parser({"message": line})
        _record_entry(state, entry)
        assert state.total is not None
        shown.append(
            progress.progress_percent(
                completed=state.completed or 0,
                total=state.total,
            )
        )

    # Same percentages the pre-refactor side-channel path rendered:
    # completed derived from the collected total, floored per cell.
    assert shown == [0, 33, 66, 100]


def test_process_dashboard_has_no_dead_update_method() -> None:
    assert not hasattr(_ProcessDashboard, "update")
