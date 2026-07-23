# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from lograil import DEFAULT_REMAPS, LogEntry, RemapPipeline
from lograil._internal import remap, tail


def test_remap_pipeline_applies_remaps_in_order() -> None:
    seen: list[str] = []

    def first(entry: LogEntry) -> LogEntry:
        seen.append("first")
        entry["message"] = "one"
        return entry

    def second(entry: LogEntry) -> LogEntry:
        seen.append("second")
        entry["message"] = f"{entry['message']} two"
        return entry

    result = RemapPipeline([first, second])({"message": "start"})

    assert seen == ["first", "second"]
    assert result == {"message": "one two"}


def test_remap_pipeline_can_drop_entries() -> None:
    def drop(entry: LogEntry) -> LogEntry | None:
        _ = entry
        return None if entry else entry

    def fail(entry: LogEntry) -> LogEntry:
        raise AssertionError("remap after drop should not run")

    assert RemapPipeline([drop, fail])({"message": "skip"}) is None


def test_remap_pipeline_shallow_copies_input_before_mutation() -> None:
    nested: dict[str, object] = {"id": 1}
    original: LogEntry = {"message": "start", "meta": nested}

    def mutate(entry: LogEntry) -> LogEntry:
        entry["message"] = "changed"
        assert entry["meta"] is nested
        return entry

    result = RemapPipeline([mutate])(original)

    assert result == {"message": "changed", "meta": nested}
    assert original == {"message": "start", "meta": nested}


def _wire_line(payload: Mapping[str, object]) -> LogEntry:
    """Wrap a JSON payload the way raw-line sources do."""
    return {
        "message": json.dumps(payload),
        "name": "stdin",
        "created": 123.0,
    }


def test_decode_ndjson_adopts_self_identifying_entries() -> None:
    payload = {
        "message": "PASSED tests.test_foo.TestX.test_y",
        "levelname": "info",
        "lograil.stage": "run",
        "lograil.stage.status": "running",
        "lograil.progress.description": "tests.test_foo.TestX.test_y",
        "lograil.progress.completed": 42,
        "lograil.progress.total": 98,
    }

    result = RemapPipeline(DEFAULT_REMAPS)(_wire_line(payload))

    assert result is not None
    assert result["message"] == "PASSED tests.test_foo.TestX.test_y"
    # normalize_entry runs after adoption.
    assert result["levelname"] == "INFO"
    assert result[remap.STAGE] == "run"
    assert result[remap.STAGE_STATUS] == "running"
    assert result[remap.PROGRESS_COMPLETED] == 42
    assert result[remap.PROGRESS_TOTAL] == 98
    # Source-provided context survives adoption.
    assert result["name"] == "stdin"
    assert result["created"] == pytest.approx(123.0)


def test_decode_ndjson_producer_context_wins_over_source() -> None:
    payload = {
        "message": "hello",
        "levelname": "INFO",
        "lograil.stage": "run",
        "name": "ggt",
        "created": 456.0,
    }

    result = RemapPipeline(DEFAULT_REMAPS)(_wire_line(payload))

    assert result is not None
    assert result["name"] == "ggt"
    assert result["created"] == pytest.approx(456.0)


def test_decode_ndjson_ignores_json_without_lograil_keys() -> None:
    raw = json.dumps({"message": "app log", "level": "info"})

    result = RemapPipeline(DEFAULT_REMAPS)({"message": raw})

    assert result is not None
    assert result["message"] == raw


def test_decode_ndjson_ignores_malformed_and_non_object_json() -> None:
    for raw in ("{not json", "{}}", '["lograil.stage"]', "12", "plain text"):
        result = RemapPipeline(DEFAULT_REMAPS)({"message": raw})
        assert result is not None
        assert result["message"] == raw


def test_decode_ndjson_allows_message_less_data_entries() -> None:
    payload = {
        "levelname": "ERROR",
        "lograil.stage": "summary",
        "ggt.detail": {"id": "tests.test_foo", "stdout": "..."},
    }

    result = RemapPipeline(DEFAULT_REMAPS)(_wire_line(payload))

    assert result is not None
    assert "message" not in result
    assert result["ggt.detail"] == {"id": "tests.test_foo", "stdout": "..."}


def test_decode_ndjson_rewrites_stale_status_detail() -> None:
    payload = {"message": "decoded", "lograil.stage": "run"}
    entry = _wire_line(payload)
    entry["lograil.status.detail"] = entry["message"]

    result = RemapPipeline(DEFAULT_REMAPS)(entry)

    assert result is not None
    assert result["lograil.status.detail"] == "decoded"


def test_decode_ndjson_indeterminate_progress_reaches_bridge() -> None:
    payload = {
        "message": "collected 123/130 tests",
        "levelname": "INFO",
        "lograil.stage": "collect",
        "lograil.stage.status": "running",
        "lograil.progress.description": "collected 123/130 tests",
        "lograil.progress.completed": 123,
        "lograil.progress.process": "ggt",
        "lograil.progress.subject": "collect",
    }

    result = RemapPipeline(DEFAULT_REMAPS)(_wire_line(payload))
    assert result is not None
    update = tail._progress_update_from_entry(result)

    assert update is not None
    assert update.completed == 123
    assert update.total is None
    assert update.process == "ggt"
    assert update.subject == "collect"
