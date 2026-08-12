# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Tests for built-in process output parsers."""

from __future__ import annotations

import json

from lograil._internal import remap
from lograil.parsers import (
    PnpmOutputParser,
    TerraformApplyOutputParser,
    TerraformPlanOutputParser,
    TurboOutputParser,
    planned_apply_addresses,
    state_resource_addresses,
)


def test_pnpm_parser_tracks_progress_and_captures_output() -> None:
    """pnpm NDJSON produces progress fields while retaining raw output."""
    parser = PnpmOutputParser()
    parser({
        "message": json.dumps({
            "name": "pnpm:progress",
            "status": "resolved",
            "packageId": "registry.npmjs.org/lodash/4.17.21",
        })
    })
    entry = parser({
        "message": json.dumps({
            "name": "pnpm:progress",
            "status": "imported",
            "to": "/tmp/.pnpm/lodash@4/node_modules/lodash",
        })
    })

    assert entry[remap.PROGRESS_DESCRIPTION] == "lodash"
    assert entry[remap.PROGRESS_COMPLETED] == 1
    assert entry[remap.PROGRESS_TOTAL] == 1
    assert parser.resolved_count == 1
    assert "pnpm:progress" in parser.captured_output


def test_pnpm_parser_ignores_malformed_and_non_object_json() -> None:
    """Malformed and non-object pnpm output remains unannotated."""
    parser = PnpmOutputParser()
    assert parser({"message": "not json"}) == {"message": "not json"}
    assert parser({"message": "[]"}) == {"message": "[]"}
    assert "not json" in parser.captured_output


def test_turbo_parser_deduplicates_tasks_and_uses_known_total() -> None:
    """Turbo cache markers advance once per package/task pair."""
    parser = TurboOutputParser(total_tasks=2)
    line = "@scope/pkg:build: cache hit, replaying output abc"
    first = parser({"message": line})
    duplicate = parser({"message": line})

    assert first[remap.PROGRESS_COMPLETED] == 1
    assert first[remap.PROGRESS_TOTAL] == 2
    assert remap.PROGRESS_COMPLETED not in duplicate
    assert parser.started_tasks == {"@scope/pkg:build"}
    assert parser.captured_output == f"{line}\n{line}"


def test_terraform_address_helpers_filter_non_managed_and_noop() -> None:
    """Terraform JSON helpers return only actionable managed resources."""
    plan = json.dumps({
        "resource_changes": [
            {
                "address": "aws_s3_bucket.changed",
                "mode": "managed",
                "change": {"actions": ["update"]},
            },
            {
                "address": "aws_s3_bucket.noop",
                "mode": "managed",
                "change": {"actions": ["no-op"]},
            },
        ]
    })
    state = json.dumps({
        "resources": [
            {
                "mode": "managed",
                "module": "module.app",
                "type": "aws_iam_role",
                "name": "role",
            },
            {"mode": "data", "address": "data.aws_region.current"},
        ]
    })

    assert planned_apply_addresses(plan) == {"aws_s3_bucket.changed"}
    assert state_resource_addresses(state) == {"module.app.aws_iam_role.role"}


def test_terraform_plan_parser_tracks_unknown_refresh_addresses() -> None:
    """Plan progress expands for refresh resources absent from prior state."""
    parser = TerraformPlanOutputParser(
        workspace="global/test",
        refresh_addresses={"aws_s3_bucket.known"},
        subject="terraform/global/test",
    )
    known = parser({"message": "aws_s3_bucket.known: Refreshing state..."})
    extra = parser({"message": "aws_s3_bucket.extra: Refreshing state..."})
    planned = parser({"message": "No changes. Your infrastructure matches"})

    assert (known[remap.PROGRESS_COMPLETED], known[remap.PROGRESS_TOTAL]) == (
        1,
        2,
    )
    assert (extra[remap.PROGRESS_COMPLETED], extra[remap.PROGRESS_TOTAL]) == (
        2,
        3,
    )
    assert planned[remap.PROGRESS_CLEAR_LABEL] is True
    assert planned[remap.PROGRESS_TOTAL] == 2


def test_terraform_apply_parser_deduplicates_terminal_events() -> None:
    """Apply completion and error events advance once per address."""
    parser = TerraformApplyOutputParser(
        workspace="global/test",
        addresses={"aws_lambda_function.a"},
        subject="terraform/global/test",
    )
    line = json.dumps({
        "type": "apply_errored",
        "hook": {"resource": {"addr": "aws_lambda_function.a"}},
    })
    first = parser({"message": line})
    duplicate = parser({"message": line})

    assert first[remap.PROGRESS_COMPLETED] == 1
    assert duplicate[remap.PROGRESS_COMPLETED] == 1
    assert parser.completed == {"aws_lambda_function.a"}
