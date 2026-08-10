from __future__ import annotations

import json

import pytest

import lograil


def entries(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines()]


def test_stage_emits_structured_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging()

    with lograil.stage(
        "source/download", process="download", subject="source.tar.gz"
    ):
        pass

    output = entries(capsys.readouterr().err)
    lifecycle = [item for item in output if "lograil.stage.status" in item]
    assert [item["lograil.stage.status"] for item in lifecycle] == [
        "started",
        "finished",
    ]
    assert all(item["lograil.stage"] == "source/download" for item in lifecycle)
    assert len(lifecycle) == len(output)


def test_nested_sticky_stage_restores_parent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging()

    with (
        lograil.stage("build", process="build", subject="pkg", sticky=True),
        lograil.stage("configure", process="configure", subject="pkg"),
    ):
        pass

    statuses = [
        item["lograil.stage.status"]
        for item in entries(capsys.readouterr().err)
        if item.get("lograil.stage") == "configure"
    ]
    assert statuses == ["started", "finished"]


def test_failed_stage_emits_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging()

    with (
        pytest.raises(RuntimeError),
        lograil.stage("build", process="build", subject="pkg"),
    ):
        raise RuntimeError("boom")

    lifecycle = [
        item
        for item in entries(capsys.readouterr().err)
        if "lograil.stage.status" in item
    ]
    assert lifecycle[-1]["lograil.stage.status"] == "failed"
    assert lifecycle[-1]["level"] == "ERROR"


@pytest.mark.parametrize("total", [None, 10])
def test_progress_emits_initial_and_final_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    total: int | None,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging()

    with lograil.progress(
        process="verify", subject="archive", description="reading", total=total
    ) as handle:
        handle.advance(2)

    updates = [
        item
        for item in entries(capsys.readouterr().err)
        if "lograil.progress.completed" in item
    ]
    assert updates[0]["lograil.progress.completed"] == 0
    assert updates[-1]["lograil.progress.completed"] == (
        total if total is not None else 2
    )
    assert len(updates) == 2
