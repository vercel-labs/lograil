# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import logging
from logging import LogRecord

import pytest
from rich.markup import render

import lograil
from lograil import DEFAULT_REMAPS, RemapPipeline
from lograil._internal import console
from lograil._internal.formatter import (
    LograilFormatter,
    _context_style,
    detail_parts,
    format_spinner_entry,
)


def test_mapping_entry_uses_generic_fields() -> None:
    entry = {
        "timestamp": "2024-01-01T12:00:00Z",
        "name": "worker.api",
        "message": "request finished",
        "levelname": "INFO",
    }

    result = lograil.format_log_entry(entry)

    assert "worker.api" in result
    assert "request finished" in result


def test_mapping_entry_does_not_infer_provider_fields() -> None:
    entry = {
        "timestamp": "2024-01-01T12:00:00Z",
        "name": "worker.api",
        "message": "booting",
        "host": "build",
    }

    result = lograil.format_log_entry(entry)

    assert "worker.api" in result
    assert "vm:build" not in result


def test_log_record_entry_uses_record_message_and_name() -> None:
    record = logging.LogRecord(
        name="app.worker",
        level=logging.WARNING,
        pathname=__file__,
        lineno=10,
        msg="retry %s",
        args=("scheduled",),
        exc_info=None,
    )

    result = lograil.format_log_entry(record)

    assert "app.worker" in result
    assert "retry scheduled" in result


def test_detail_parts_are_generic_extras() -> None:
    entry = {
        "name": "app.worker",
        "message": "starting task",
        "levelname": "INFO",
        "pid": 54,
        "queue": "default",
    }

    assert detail_parts(entry) == ["pid=54", "queue=default"]
    result = lograil.format_log_entry(entry, include_extra=True)
    assert "pid=54" in result
    assert "queue=default" in result


def test_mapping_entry_normalization_is_a_remap() -> None:
    entry = {"timestamp": "2024-01-01T12:00:00Z", "msg": "ready"}

    normalized = RemapPipeline(DEFAULT_REMAPS)(entry)

    assert normalized is not None
    assert normalized["message"] == "ready"
    assert normalized["levelname"] == "INFO"
    assert "ready" in lograil.format_log_entry(normalized)


def test_plain_formatter_escapes_markup_message() -> None:
    record = LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="failed [/etc/config] [bold]literal[/bold]",
        args=(),
        exc_info=None,
    )

    result = LograilFormatter(output_mode="plain").format(record)

    assert "\\[/etc/config]" in result
    assert "\\[bold]literal" in result


def test_plain_formatter_renderable_interprets_ansi_message() -> None:
    record = LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=(
            "[gw5]\x1b[36m [  2%] \x1b[0m\x1b[32mPASSED\x1b[0m test.py::test_ok"
        ),
        args=(),
        exc_info=None,
    )

    result = LograilFormatter(output_mode="plain").format_renderable(record)

    assert not isinstance(result, str)
    assert result.plain.endswith("[gw5] [  2%] PASSED test.py::test_ok")
    assert result.spans


def test_plain_formatter_renderable_does_not_parse_markup_message() -> None:
    record = LogRecord(
        name="app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="failed [/etc/config] [bold]literal[/bold]",
        args=(),
        exc_info=None,
    )

    result = LograilFormatter(output_mode="plain").format_renderable(record)

    assert not isinstance(result, str)
    assert result.plain.endswith("failed [/etc/config] [bold]literal[/bold]")


def test_oneline_truncates_to_console_width_before_escaping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 20)
    result = lograil.format_log_entry(
        {"timestamp": "12:00", "message": "[" + "x" * 100},
        oneline=True,
        context=None,
    )

    assert result.endswith("->")


def test_oneline_truncates_wide_characters_to_console_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 20)

    result = lograil.format_log_entry(
        {"message": "界" * 20},
        oneline=True,
        context=None,
    )

    assert console.stdout_console.measure(result).maximum <= 20
    assert result.endswith("->")


def test_wrap_long_token_with_markup_does_not_split_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 80)
    message = "x" * 68 + "[/red]" + "y" * 200

    result = lograil.format_log_entry({"message": message}, context=None)

    rendered = render(result)  # must not raise MarkupError
    assert "".join(rendered.plain.split()) == message
    assert "\\" not in rendered.plain
    assert not rendered.spans


def test_wrap_escapes_markup_in_wrapped_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 40)
    message = "word " * 10 + "[bold]styled[/bold] " + "tail " * 10

    result = lograil.format_log_entry(
        {"message": message.strip()}, context=None
    )

    rendered = render(result)  # must not raise MarkupError
    assert "[bold]styled[/bold]" in rendered.plain
    assert not rendered.spans


def test_format_log_entry_width_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 40)
    message = ("word " * 30).strip()

    default = lograil.format_log_entry({"message": message}, context=None)
    wide = lograil.format_log_entry(
        {"message": message}, context=None, width=500
    )

    assert "\n" in default
    assert "\n" not in wide


def test_format_log_entry_oneline_width_override() -> None:
    result = lograil.format_log_entry(
        {"message": "x" * 100},
        oneline=True,
        context=None,
        width=20,
    )

    assert result.endswith("->")
    assert console.stdout_console.measure(result).maximum <= 20


def test_context_style_cache_tracks_color_system() -> None:
    truecolor = _context_style("svc", "truecolor")
    basic = _context_style("svc", None)

    assert "rgb(" in truecolor
    assert "rgb(" not in basic


def test_format_log_entry_escapes_context_name_markup() -> None:
    result = lograil.format_log_entry({"name": "scan[/tmp]", "message": "ok"})

    rendered = render(result)
    assert "scan[/tmp] ok" in rendered.plain


def test_format_spinner_entry_escapes_context_name_markup() -> None:
    result = format_spinner_entry(
        {"name": "a[bold]b", "levelname": "INFO"},
        "ok",
        show_context=True,
    )

    rendered = render(result)
    assert "a[bold]b ok" in rendered.plain


def test_format_log_entry_wrap_preserves_embedded_newlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(console.stdout_console, "width", 48)
    message = 'Traceback:\n  File "app.py", line 1, in <module> ' + "x" * 80

    result = lograil.format_log_entry({"message": message}, context=None)
    rendered = render(result)

    assert "Traceback:\n  File" in rendered.plain
