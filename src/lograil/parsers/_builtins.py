# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Built-in process output parser registrations."""

from __future__ import annotations

from lograil.parsers._base import (
    OutputParserCapabilities,
    register_output_parser,
)
from lograil.parsers.generic import GenericOutputParser
from lograil.parsers.pnpm import PnpmOutputParser
from lograil.parsers.pytest import PytestOutputParser
from lograil.parsers.terraform import (
    TerraformApplyOutputParser,
    TerraformPlanOutputParser,
)
from lograil.parsers.turbo import TurboOutputParser

register_output_parser("generic", GenericOutputParser)
register_output_parser(
    "pnpm",
    PnpmOutputParser,
    capabilities=OutputParserCapabilities(starts_progress=True),
    command_names=("pnpm",),
)
register_output_parser(
    "pytest",
    PytestOutputParser,
    capabilities=OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    ),
    command_names=("pytest", "py.test"),
)
register_output_parser(
    "turbo",
    TurboOutputParser,
    capabilities=OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    ),
    command_names=("turbo",),
)
register_output_parser(
    "terraform-plan",
    lambda: TerraformPlanOutputParser(workspace="terraform"),
    capabilities=OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    ),
)
register_output_parser(
    "terraform-apply",
    lambda: TerraformApplyOutputParser(workspace="terraform"),
    capabilities=OutputParserCapabilities(
        starts_progress=True,
        complete_on_success=True,
    ),
)
