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
from lograil.parsers.pnpm import PnpmOutputParser, extract_package_name
from lograil.parsers.terraform import (
    TerraformApplyOutputParser,
    TerraformPlanOutputParser,
    is_plan_live_progress_line,
    planned_apply_addresses,
    state_resource_addresses,
    terraform_event_address,
    terraform_planned_change_address,
)
from lograil.parsers.turbo import TurboOutputParser

__all__ = [
    "OutputParserCapabilities",
    "PnpmOutputParser",
    "ProcessOutputParser",
    "TerraformApplyOutputParser",
    "TerraformPlanOutputParser",
    "TurboOutputParser",
    "extract_package_name",
    "is_plan_live_progress_line",
    "planned_apply_addresses",
    "register_output_parser",
    "state_resource_addresses",
    "terraform_event_address",
    "terraform_planned_change_address",
]
