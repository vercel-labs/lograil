# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""pnpm NDJSON process output parser."""

from __future__ import annotations

from typing import Any

import json

from lograil._internal import remap
from lograil._internal.tail import LogEntry


class PnpmOutputParser:
    """Annotate pnpm's NDJSON reporter output with install progress."""

    def __init__(self) -> None:
        """Create an isolated parser for one pnpm process."""
        self.resolved_count = 0
        self.store_count = 0
        self.imported_count = 0
        self.phase = ""
        self._captured_output: list[str] = []

    @property
    def captured_output(self) -> str:
        """Every raw line observed by this parser."""
        return "\n".join(self._captured_output)

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate one pnpm output entry."""
        message = entry.get("message")
        if not isinstance(message, str):
            return entry
        self._captured_output.append(message)
        try:
            raw = json.loads(message)
        except ValueError:
            return entry
        if not isinstance(raw, dict):
            return entry

        name = raw.get("name")
        if name == "pnpm:stage":
            stage = raw.get("message")
            if isinstance(stage, str):
                self.phase = stage
                entry["lograil.status.detail"] = stage
            return entry
        if name != "pnpm:progress":
            return entry

        status = raw.get("status")
        if status == "resolved":
            self.resolved_count += 1
        elif status == "found_in_store":
            self.store_count += 1
        elif status == "imported":
            self.imported_count += 1
        else:
            return entry

        package = extract_package_name(raw)
        total = self.resolved_count + self.store_count
        if self.imported_count:
            completed = self.store_count + self.imported_count
            description = package or "importing…"
            entry[remap.PROGRESS_TOTAL] = total
        else:
            completed = total
            description = package or "resolving…"
            entry.pop(remap.PROGRESS_TOTAL, None)
        entry["lograil.status.detail"] = description
        entry[remap.PROGRESS_DESCRIPTION] = description
        entry[remap.PROGRESS_COMPLETED] = completed
        return entry


def extract_package_name(message: dict[str, Any]) -> str | None:
    """Extract a human-readable package name from a pnpm event."""
    package_id = message.get("packageId")
    if isinstance(package_id, str):
        parts = package_id.split("/")
        if len(parts) >= 4 and parts[-3].startswith("@"):
            return f"{parts[-3]}/{parts[-2]}"
        if len(parts) >= 3:
            return parts[-2]

    to_path = message.get("to")
    if isinstance(to_path, str):
        segments = to_path.replace("\\", "/").split("/")
        if len(segments) >= 2 and segments[-2].startswith("@"):
            return f"{segments[-2]}/{segments[-1]}"
        if segments:
            return segments[-1]
    return None
