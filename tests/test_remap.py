# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from lograil import LogEntry, RemapPipeline


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
