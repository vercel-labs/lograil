# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Built-in process output parser registrations."""

from __future__ import annotations

from lograil.parsers._base import (
    OutputParserCapabilities,
    register_output_parser,
)
from lograil.parsers.generic import GenericOutputParser
from lograil.parsers.pytest import PytestOutputParser

register_output_parser("generic", GenericOutputParser)
register_output_parser(
    "pytest",
    PytestOutputParser,
    capabilities=OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    ),
    command_names=("pytest", "py.test"),
)
