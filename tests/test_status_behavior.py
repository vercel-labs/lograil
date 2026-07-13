# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest
from rich.text import Text

import lograil
from lograil._internal import log as _log, progress


def test_status_json_mode_preserves_ndjson_records(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    logger = lograil.configure_logging(default="debug")

    with lograil.status("working"):
        logger.info("inside")
        logger.warning("warn")

    messages = [
        json.loads(line)["message"]
        for line in capsys.readouterr().err.splitlines()
    ]
    assert "working" in messages
    assert "inside" in messages
    assert "warn" in messages


def test_update_status_subject_only_without_active_status_logs_plain(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging(default="debug")

    lograil.update_status(subject="building")

    payload = json.loads(capsys.readouterr().err)
    assert payload["message"] == "building"


def test_status_flushes_warning_on_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    logger = lograil.configure_logging(default="debug")

    with lograil.status("working", done=None):
        logger.warning("disk almost full")

    assert "disk almost full" in capsys.readouterr().err


def test_spinner_status_escapes_literal_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    logger = lograil.configure_logging(default="debug")

    with lograil.status("working [/etc/config]", done=None):
        logger.info("failed [/etc/config]")


def test_fancy_status_process_subject_renders_styling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    lograil.configure_logging(default="debug")

    with lograil.status(
        process="docker", subject="building", done=None
    ) as handle:
        assert handle._status is not None
        spinner_text = handle._status.status

    assert isinstance(spinner_text, Text)
    assert spinner_text.plain == "docker building"
    assert "[bold blue]" not in spinner_text.plain
    assert any("bold blue" in str(span.style) for span in spinner_text.spans)


def test_fancy_status_message_brackets_display_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    lograil.configure_logging(default="debug")

    with lograil.status("working [/etc/config]", done=None) as handle:
        assert handle._status is not None
        spinner_text = handle._status.status

    assert isinstance(spinner_text, Text)
    assert spinner_text.plain == "working [/etc/config]"
    assert "\\" not in spinner_text.plain


def test_spinner_log_message_with_markup_round_trips_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    logger = lograil.configure_logging(default="debug")

    with lograil.status("working", done=None) as handle:
        logger.info("failed [/etc/config] [bold]literal[/bold]")
        assert handle._status is not None
        spinner_text = handle._status.status

    assert isinstance(spinner_text, Text)
    assert spinner_text.plain == "failed [/etc/config] [bold]literal[/bold]"
    assert "\\" not in spinner_text.plain
    assert not spinner_text.spans


@pytest.mark.parametrize("mode", ["json", "fancy"])
def test_update_status_in_non_sticky_status_does_not_leak_sticky(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", mode)
    lograil.configure_logging(default="debug")
    assert _log.get_sticky_prefix() is None

    stderr = io.StringIO()
    with (
        patch("lograil._internal.console.stderr_console.print"),
        patch("lograil._internal.log.sys.stderr", stderr),
        lograil.status("working", done=None),
    ):
        lograil.update_status(subject="step 2")

    assert _log.get_sticky_prefix() is None
    assert _log.get_sticky_process() is None
    assert _log.get_sticky_subject() is None


def test_status_after_non_sticky_update_status_is_unprefixed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging(default="debug")

    with lograil.status("working", done=None):
        lograil.update_status(subject="step 2")
    with lograil.status("next", done=None):
        pass

    messages = [
        json.loads(line)["message"]
        for line in capsys.readouterr().err.splitlines()
    ]
    assert "next" in messages
    assert not any(message.startswith("step 2:") for message in messages)


def test_update_status_subject_only_inside_unstructured_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging(default="debug")

    stderr = io.StringIO()
    with (
        patch("lograil._internal.log.sys.stderr", stderr),
        lograil.status("working", done=None),
    ):
        lograil.update_status(subject="step 2")

    messages = [
        json.loads(line)["message"] for line in stderr.getvalue().splitlines()
    ]
    assert "step 2" in messages


def test_status_done_message_is_plain_in_json_mode(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    lograil.configure_logging(default="debug")

    with lograil.status(process="docker", subject="building"):
        pass

    messages = [
        json.loads(line)["message"]
        for line in capsys.readouterr().err.splitlines()
    ]
    assert any(
        message.endswith("docker building: done") for message in messages
    )
    assert not any("[bold blue]" in message for message in messages)


def test_clear_label_stops_owned_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "fancy")
    lograil.configure_logging(default="debug")
    renderer = progress.StatusProgressRenderer()

    renderer.update(progress.ProgressUpdate("build", 1, 2))
    assert renderer.active is True

    renderer.update(progress.ProgressUpdate("build", 1, 2, clear_label=True))

    assert renderer.active is False
    renderer.finish()
