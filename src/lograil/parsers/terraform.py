# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Terraform plan and apply process output parsers."""

from __future__ import annotations

from typing import Any

import json
import re

from lograil._internal import remap
from lograil._internal.tail import LogEntry

_PLAN_NOOP_ACTIONS = {(), ("no-op",)}
_APPLY_TERMINAL_EVENT_TYPES = {"apply_complete", "apply_errored"}
_REFRESH_EVENT_TYPES = {"refresh_complete", "refresh_errored"}
_PLAN_READING_TEXT_RE = re.compile(r"^(?P<address>\S+): Reading\.\.\.")
_PLAN_READ_COMPLETE_TEXT_RE = re.compile(
    r"^(?P<address>\S+): Read complete(?: after .*)?"
)
_PLAN_REFRESHING_TEXT_RE = re.compile(
    r"^(?P<address>\S+): Refreshing state\.\.\."
)
_PLAN_REFRESH_COMPLETE_TEXT_RE = re.compile(
    r"^(?P<address>\S+): Refresh complete(?: after .*)?"
)


def is_plan_live_progress_line(line: str) -> bool:
    """Return whether a Terraform plan line is transient live progress."""
    return any(
        pattern.match(line) is not None
        for pattern in (
            _PLAN_READING_TEXT_RE,
            _PLAN_READ_COMPLETE_TEXT_RE,
            _PLAN_REFRESHING_TEXT_RE,
            _PLAN_REFRESH_COMPLETE_TEXT_RE,
        )
    )


def planned_apply_addresses(plan_json_text: str) -> set[str]:
    """Return managed resource addresses changed by a Terraform plan."""
    try:
        plan = json.loads(plan_json_text)
    except ValueError:
        return set()
    if not isinstance(plan, dict):
        return set()
    changes = plan.get("resource_changes")
    if not isinstance(changes, list):
        return set()
    addresses: set[str] = set()
    for item in changes:
        if not isinstance(item, dict) or item.get("mode") not in {
            None,
            "managed",
        }:
            continue
        change = item.get("change")
        if not isinstance(change, dict):
            continue
        raw_actions = change.get("actions")
        if not isinstance(raw_actions, list):
            continue
        actions = tuple(str(action) for action in raw_actions)
        if actions in _PLAN_NOOP_ACTIONS:
            continue
        address = item.get("address")
        if isinstance(address, str) and address:
            addresses.add(address)
    return addresses


def state_resource_addresses(state_json_text: str) -> set[str]:
    """Return managed resource addresses from Terraform state JSON."""
    try:
        state = json.loads(state_json_text)
    except ValueError:
        return set()
    if not isinstance(state, dict):
        return set()
    resources = state.get("resources")
    if not isinstance(resources, list):
        return set()
    addresses: set[str] = set()
    for resource in resources:
        if not isinstance(resource, dict) or resource.get("mode") not in {
            None,
            "managed",
        }:
            continue
        address = resource.get("address")
        if isinstance(address, str) and address:
            addresses.add(address)
            continue
        resource_type = resource.get("type")
        name = resource.get("name")
        if isinstance(resource_type, str) and isinstance(name, str):
            address = f"{resource_type}.{name}"
            prefix = resource.get("module")
            if isinstance(prefix, str) and prefix:
                address = f"{prefix}.{address}"
            addresses.add(address)
    return addresses


def terraform_event_address(event: dict[str, Any]) -> str | None:
    """Extract a resource address from a Terraform JSON event."""
    hook = event.get("hook")
    if isinstance(hook, dict):
        resource = hook.get("resource")
        if isinstance(resource, dict):
            address = resource.get("addr") or resource.get("addr_abs")
            if isinstance(address, str) and address:
                return address
        address = hook.get("resource_addr") or hook.get("addr")
        if isinstance(address, str) and address:
            return address
    address = event.get("resource_addr") or event.get("addr")
    return address if isinstance(address, str) and address else None


def terraform_planned_change_address(event: dict[str, Any]) -> str | None:
    """Extract a resource address from a planned-change event."""
    change = event.get("change")
    if not isinstance(change, dict):
        return None
    resource = change.get("resource")
    if not isinstance(resource, dict):
        return None
    address = resource.get("addr") or resource.get("addr_abs")
    return address if isinstance(address, str) and address else None


def _annotate(
    entry: LogEntry,
    description: str,
    *,
    completed: int,
    total: int,
    process: str | None = None,
    subject: str | None = None,
    clear_label: bool = False,
) -> LogEntry:
    entry["lograil.status.detail"] = description
    entry[remap.PROGRESS_DESCRIPTION] = description
    entry[remap.PROGRESS_COMPLETED] = min(completed, total)
    entry[remap.PROGRESS_TOTAL] = total
    if process is not None:
        entry[remap.PROGRESS_PROCESS] = process
    if subject is not None:
        entry[remap.PROGRESS_SUBJECT] = subject
    if clear_label:
        entry[remap.PROGRESS_CLEAR_LABEL] = True
    return entry


class TerraformPlanOutputParser:
    """Annotate Terraform plan text with read and refresh progress."""

    def __init__(
        self,
        *,
        workspace: str,
        refresh_addresses: set[str] | None = None,
        planned_addresses: set[str] | None = None,
        subject: str | None = None,
    ) -> None:
        """Create a parser for one Terraform plan."""
        self.workspace = workspace
        self.refresh_addresses = refresh_addresses or set()
        self.planned_addresses = planned_addresses or set()
        self.subject = subject
        self.refreshed: set[str] = set()
        self.reading: set[str] = set()
        self.read: set[str] = set()
        self.planning_started = False

    @property
    def total(self) -> int:
        """Current progress total, including unknown resources."""
        extra_refresh_slot = 0 if self.planning_started else 1
        return max(
            len(self.refresh_addresses),
            len(self.refreshed) + len(self.read) + extra_refresh_slot,
            1,
        )

    def start_entry(self) -> LogEntry:
        """Return the initial planning progress entry."""
        return _annotate(
            {"message": "Planning Terraform"},
            "Planning Terraform",
            completed=0,
            total=self.total,
            process="planning",
            subject=self.subject,
        )

    def finish_entry(self) -> LogEntry:
        """Return the terminal planning progress entry."""
        return _annotate(
            {"message": f"Planning {self.workspace}"},
            f"Planning {self.workspace}",
            completed=self.total,
            total=self.total,
            clear_label=True,
        )

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate one Terraform plan output entry."""
        message = entry.get("message")
        if not isinstance(message, str):
            return entry
        reading_match = _PLAN_READING_TEXT_RE.match(message)
        if reading_match is not None:
            address = reading_match.group("address")
            self.reading.add(address)
            return _annotate(
                entry,
                f"Reading {self.workspace}: {address}",
                completed=len(self.refreshed) + len(self.read),
                total=self.total,
                process="reading",
                subject=self.subject,
            )
        read_match = _PLAN_READ_COMPLETE_TEXT_RE.match(message)
        if read_match is not None:
            address = read_match.group("address")
            self.reading.discard(address)
            self.read.add(address)
            return _annotate(
                entry,
                f"Read {self.workspace}: {address}",
                completed=len(self.refreshed) + len(self.read),
                total=self.total,
                process="reading",
                subject=self.subject,
            )
        refreshing_match = _PLAN_REFRESHING_TEXT_RE.match(message)
        if refreshing_match is not None:
            address = refreshing_match.group("address")
            self.refreshed.add(address)
            return _annotate(
                entry,
                f"Refreshing {self.workspace}: {address}",
                completed=len(self.refreshed) + len(self.read),
                total=self.total,
                process="refreshing",
                subject=self.subject,
            )
        refresh_match = _PLAN_REFRESH_COMPLETE_TEXT_RE.match(message)
        if refresh_match is not None:
            address = refresh_match.group("address")
            self.refreshed.add(address)
            return _annotate(
                entry,
                f"Refreshed {self.workspace}: {address}",
                completed=len(self.refreshed) + len(self.read),
                total=self.total,
                process="refreshing",
                subject=self.subject,
            )
        if not self.planning_started and message.startswith((
            "Terraform will perform",
            "No changes.",
        )):
            self.planning_started = True
            return self.finish_entry() | {"message": message}
        return entry


class TerraformApplyOutputParser:
    """Annotate Terraform apply JSON events with resource progress."""

    def __init__(
        self,
        *,
        workspace: str,
        addresses: set[str] | None = None,
        subject: str | None = None,
    ) -> None:
        """Create a parser for one Terraform apply."""
        self.workspace = workspace
        self.addresses = addresses or set()
        self.subject = subject
        self.completed: set[str] = set()
        self.refresh_seen: set[str] = set()

    @property
    def total(self) -> int:
        """Current apply resource total."""
        return max(len(self.addresses), 1)

    def start_entry(self) -> LogEntry:
        """Return the initial apply progress entry."""
        return _annotate(
            {"message": "Applying Terraform"},
            "Applying Terraform",
            completed=0,
            total=self.total,
            process="applying",
            subject=self.subject,
        )

    def finish_entry(self) -> LogEntry:
        """Return the terminal apply progress entry."""
        return _annotate(
            {"message": ""},
            "",
            completed=self.total,
            total=self.total,
            clear_label=True,
        )

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Annotate one Terraform JSON output entry."""
        message = entry.get("message")
        if not isinstance(message, str):
            return entry
        try:
            event = json.loads(message)
        except ValueError:
            return entry
        if not isinstance(event, dict):
            return entry
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return entry

        address = terraform_event_address(event)
        if event_type == "planned_change":
            address = terraform_planned_change_address(event)
            if address is None:
                return entry
            self.addresses.add(address)
            return _annotate(
                entry,
                address,
                completed=len(self.completed),
                total=self.total,
                process="applying",
                subject=self.subject,
            )
        if event_type in _REFRESH_EVENT_TYPES and address:
            self.refresh_seen.add(address)
            return _annotate(
                entry,
                f"Refreshing {self.workspace}: {address}",
                completed=len(self.completed),
                total=self.total,
                process="refreshing",
                subject=self.subject,
            )
        if event_type not in _APPLY_TERMINAL_EVENT_TYPES or address is None:
            return entry
        self.addresses.add(address)
        self.completed.add(address)
        return _annotate(
            entry,
            address,
            completed=len(self.completed),
            total=self.total,
            process="applying",
            subject=self.subject,
        )
