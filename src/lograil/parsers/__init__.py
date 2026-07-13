# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Process output parsers."""

from __future__ import annotations

from lograil.parsers import _builtins as _builtins
from lograil.parsers._base import (
    OutputParserCapabilities,
    ProcessOutputParser,
    register_output_parser,
)

__all__ = [
    "OutputParserCapabilities",
    "ProcessOutputParser",
    "register_output_parser",
]
