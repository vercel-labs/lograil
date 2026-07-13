# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import logging

import pytest

import lograil
from lograil._internal import log


def test_env_filter_defaults_to_info_for_empty_or_invalid_spec() -> None:
    for spec in (None, "", "not-a-level"):
        env_filter = lograil.EnvFilter(spec)

        assert env_filter.enabled_for("lograil", logging.INFO) is True
        assert env_filter.enabled_for("lograil", logging.DEBUG) is False


def test_env_filter_bare_levels_apply_to_all_loggers() -> None:
    env_filter = lograil.EnvFilter("warn")

    assert env_filter.enabled_for("lograil", logging.WARNING) is True
    assert env_filter.enabled_for("other", logging.INFO) is False


def test_env_filter_target_directives_match_segment_prefixes() -> None:
    env_filter = lograil.EnvFilter("my_app.module=debug")

    assert env_filter.enabled_for("my_app.module", logging.DEBUG) is True
    assert env_filter.enabled_for("my_app.module.worker", logging.DEBUG) is True
    assert env_filter.enabled_for("my_app.modules", logging.DEBUG) is False


def test_env_filter_target_only_directive_enables_trace() -> None:
    env_filter = lograil.EnvFilter("my_app.module")

    assert env_filter.enabled_for("my_app.module", logging.DEBUG) is True


def test_env_filter_specific_directive_overrides_later_broader() -> None:
    env_filter = lograil.EnvFilter("my_app.worker=debug,my_app=error")

    assert env_filter.enabled_for("my_app.worker", logging.DEBUG) is True
    assert env_filter.enabled_for("my_app.other", logging.WARNING) is False


def test_env_filter_later_equal_specificity_directive_overrides() -> None:
    env_filter = lograil.EnvFilter("my_app=debug,my_app=error")

    assert env_filter.enabled_for("my_app", logging.WARNING) is False
    assert env_filter.enabled_for("my_app", logging.ERROR) is True


def test_env_filter_off_disables_target() -> None:
    env_filter = lograil.EnvFilter("debug,my_app.noisy=off")

    assert env_filter.enabled_for("my_app.noisy", logging.CRITICAL) is False
    assert env_filter.enabled_for("my_app.noisy.child", logging.ERROR) is False
    assert env_filter.enabled_for("my_app.quiet", logging.DEBUG) is True


def test_env_filter_trace_maps_to_debug() -> None:
    env_filter = lograil.EnvFilter("trace")

    assert env_filter.enabled_for("lograil", logging.DEBUG) is True


def test_env_filter_ignores_unsupported_span_and_field_directives() -> None:
    env_filter = lograil.EnvFilter("[span]=debug,lograil{field=value}=trace")

    assert env_filter.enabled_for("lograil", logging.INFO) is True
    assert env_filter.enabled_for("lograil", logging.DEBUG) is False


def test_configure_logging_uses_app_specific_envvar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL", raising=False)
    monkeypatch.setenv("APP_LOG", "lograil=debug")

    logger = lograil.configure_logging(envvar="APP_LOG")

    try:
        assert logger.name == "lograil"
        assert logger.isEnabledFor(logging.DEBUG) is True
    finally:
        lograil.configure_logging(default="info")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "plain"),
        ("json", "json"),
        ("fancy", "fancy"),
    ],
)
def test_configure_logging_reads_output_mode(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", value)

    lograil.configure_logging()

    try:
        assert log.output_mode() == expected
        assert log.plain_output_enabled() is (expected == "plain")
    finally:
        monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
        lograil.configure_logging(default="info")


def test_configure_logging_defaults_output_from_interactive_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
    monkeypatch.setattr("sys.stderr.isatty", lambda: True)

    lograil.configure_logging()

    try:
        assert log.output_mode() == "fancy"
    finally:
        lograil.configure_logging(default="info")


def test_json_output_mode_renders_log_records_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LOGRAIL_OUTPUT", "json")
    logger = lograil.configure_logging(default="debug")

    try:
        logger.debug("hello %s", "world")
        captured = capsys.readouterr()
        payload = json.loads(captured.err)
        assert payload["level"] == "DEBUG"
        assert payload["logger"] == "lograil"
        assert payload["message"] == "hello world"
    finally:
        monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
        lograil.configure_logging(default="info")
