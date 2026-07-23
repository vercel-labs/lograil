# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from lograil import (
    format_progress_line,
    lograil_instrumentation_env,
)
from lograil._internal import console, log, process, progress


def test_format_and_parse_structured_status() -> None:
    line = format_progress_line(
        description="Uploading bundle part 1",
        process="uploading",
        subject="release/app",
        completed=1,
        total=2,
    )

    parsed = progress.parse(line)

    assert parsed is not None
    assert parsed.description == "Uploading bundle part 1"
    assert parsed.process == "uploading"
    assert parsed.subject == "release/app"
    assert parsed.completed == 1
    assert parsed.total == 2


def test_parse_rejects_bad_payload() -> None:
    assert progress.parse("::lograil-progress::[]") is None
    assert progress.parse("not progress") is None


def test_format_line_omits_indeterminate_total() -> None:
    line = format_progress_line(description="collecting", completed=5)

    assert '"total"' not in line
    parsed = progress.parse(line)
    assert parsed is not None
    assert parsed.completed == 5
    assert parsed.total is None


def test_from_mapping_accepts_missing_total() -> None:
    update = progress.ProgressUpdate.from_mapping({
        "description": "collecting",
        "completed": 5,
    })

    assert update is not None
    assert update.total is None


def test_from_mapping_still_validates_types() -> None:
    assert (
        progress.ProgressUpdate.from_mapping({
            "description": "collecting",
            "completed": "5",
        })
        is None
    )
    assert (
        progress.ProgressUpdate.from_mapping({
            "description": "collecting",
            "completed": 5,
            "total": "10",
        })
        is None
    )


def test_instrumentation_env_enables_progress_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = lograil_instrumentation_env()

    assert isinstance(env, dict)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    assert progress.should_emit_progress_lines() is True


def test_status_progress_uses_unicode_glyphs_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stderr_console, "legacy_windows", False)

    message = progress._format_status_progress(
        description="building",
        detail="step output",
        completed=1,
        total=2,
    )

    assert "━" in message.plain
    assert "─" in message.plain


def test_status_progress_falls_back_to_ascii_glyphs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stderr_console, "legacy_windows", True)

    message = progress._format_status_progress(
        description="building",
        detail="step output",
        completed=1,
        total=2,
    )

    assert "=" in message.plain
    assert "-" in message.plain


def test_status_progress_indeterminate_renders_detail_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stderr_console, "legacy_windows", False)

    message = progress._format_status_progress(
        description="collecting",
        detail="collected 123/130 tests",
        completed=123,
        total=None,
    )

    assert "collected 123/130 tests" in message.plain
    assert "━" not in message.plain
    assert "%" not in message.plain


class _FakeProgress:
    """Stand-in for rich.progress.Progress recording stop() calls."""

    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _install_owned_display(
    renderer: progress.StatusProgressRenderer,
) -> _FakeProgress:
    fake = _FakeProgress()
    renderer._progress = fake  # type: ignore[assignment]
    renderer._task_id = 0  # type: ignore[assignment]
    renderer._owns_live = True
    renderer._active = True
    return fake


def _renderer_reset_state(
    renderer: progress.StatusProgressRenderer,
) -> tuple[object, object, bool, bool]:
    return (
        renderer._progress,
        renderer._task_id,
        renderer._owns_live,
        renderer._active,
    )


def test_clear_label_and_finish_reset_identical_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(log, "plain_output_enabled", lambda: False)
    monkeypatch.setattr(log, "get_active_status", lambda: None)
    monkeypatch.setattr(log, "get_sticky_prefix", lambda: None)

    cleared = progress.StatusProgressRenderer()
    cleared_fake = _install_owned_display(cleared)
    cleared.update(
        progress.ProgressUpdate(
            description="done",
            completed=1,
            total=1,
            clear_label=True,
        )
    )

    finished = progress.StatusProgressRenderer()
    finished_fake = _install_owned_display(finished)
    finished.finish()

    assert cleared_fake.stopped is True
    assert finished_fake.stopped is True
    assert _renderer_reset_state(cleared) == _renderer_reset_state(finished)
    assert _renderer_reset_state(finished) == (None, None, False, False)
    assert cleared.active is False
    assert finished.active is False
    # finish() additionally closes the renderer and drops the status.
    assert finished._closed is True
    assert finished._active_status is None


def test_renderer_indeterminate_to_determinate_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(log, "plain_output_enabled", lambda: False)
    monkeypatch.setattr(log, "get_active_status", lambda: None)
    monkeypatch.setattr(log, "get_sticky_prefix", lambda: None)
    monkeypatch.setattr(log, "get_sticky_subject", lambda: None)

    renderer = progress.StatusProgressRenderer()
    try:
        renderer.update(
            progress.ProgressUpdate(
                description="collected 5 tests",
                completed=5,
                label="ggt collect",
            )
        )
        assert renderer._progress is not None
        assert renderer._task_id is not None
        task = renderer._progress.tasks[renderer._task_id]
        assert task.total is None

        renderer.update(
            progress.ProgressUpdate(
                description="running tests",
                completed=1,
                total=10,
                label="ggt run",
            )
        )
        task = renderer._progress.tasks[renderer._task_id]
        assert task.total == 10
        assert task.completed == 1
    finally:
        renderer.finish()


def test_progress_bar_width_is_single_sourced() -> None:
    assert progress.PROGRESS_BAR_WIDTH == 20
    assert progress._PROGRESS_BAR_WIDTH == progress.PROGRESS_BAR_WIDTH
    assert not hasattr(process, "_PROGRESS_BAR_WIDTH")


def test_render_progress_bar_defaults_to_shared_width() -> None:
    bar = progress.render_progress_bar(completed=1, total=2)

    assert bar.cell_len == progress.PROGRESS_BAR_WIDTH
