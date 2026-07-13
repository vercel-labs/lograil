# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Generic process output parser."""

from __future__ import annotations

from lograil._internal.tail import LogEntry


class GenericOutputParser:
    """Default parser for unstructured process output."""

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate ``entry`` with a status detail when possible."""
        message = entry.get("message")
        if isinstance(message, str) and message:
            entry["lograil.status.detail"] = message
        return entry
