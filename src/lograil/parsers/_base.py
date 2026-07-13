# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Process output parser registry."""

from __future__ import annotations

from typing import Protocol

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from lograil._internal.async_tail import StreamMode
from lograil._internal.tail import LogEntry


class ProcessOutputParser(Protocol):
    """Callable that annotates a process output entry."""

    def __call__(self, entry: LogEntry) -> LogEntry:
        """Return an annotated entry."""


@dataclass(frozen=True, slots=True)
class OutputParserCapabilities:
    """Rendering capabilities declared by a process output parser."""

    starts_progress: bool = False
    complete_on_success: bool = False


@dataclass(frozen=True, slots=True)
class OutputParserSpec:
    """Process metadata used for parser auto-detection."""

    argv: Sequence[str]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    name: str | None = None
    process: str | None = None
    subject: str | None = None
    category: str | None = None
    stream: StreamMode = "stderr"
    kind: str | None = None


OutputParserFactory = Callable[[], ProcessOutputParser]
OutputParserPredicate = Callable[[OutputParserSpec], bool]


@dataclass(frozen=True, slots=True)
class OutputParserBinding:
    """Resolved parser instance and its metadata."""

    name: str | None
    parser: ProcessOutputParser
    capabilities: OutputParserCapabilities


@dataclass(frozen=True, slots=True)
class _OutputParserRegistration:
    name: str
    factory: OutputParserFactory
    capabilities: OutputParserCapabilities
    command_names: frozenset[str]
    predicate: OutputParserPredicate | None
    priority: int
    order: int


_registry: dict[str, _OutputParserRegistration] = {}
_next_order = 0
_NO_CAPABILITIES = OutputParserCapabilities()


def register_output_parser(
    name: str,
    factory: OutputParserFactory,
    *,
    capabilities: OutputParserCapabilities | None = None,
    command_names: Iterable[str] = (),
    predicate: OutputParserPredicate | None = None,
    priority: int = 0,
    replace: bool = False,
) -> None:
    """Register a named process output parser."""
    global _next_order  # noqa: PLW0603
    normalized = name.strip()
    if not normalized:
        msg = "output parser name must not be empty"
        raise ValueError(msg)
    if not replace and normalized in _registry:
        msg = f"output parser '{normalized}' is already registered"
        raise ValueError(msg)
    registration = _OutputParserRegistration(
        name=normalized,
        factory=factory,
        capabilities=capabilities or _NO_CAPABILITIES,
        command_names=frozenset(command_names),
        predicate=predicate,
        priority=priority,
        order=_next_order,
    )
    _next_order += 1
    _registry[normalized] = registration


def get_output_parser(name: str) -> OutputParserBinding:
    """Return a new parser instance for ``name``."""
    try:
        registration = _registry[name]
    except KeyError:
        known = ", ".join(sorted(_registry)) or "none"
        msg = f"unknown output parser '{name}' (known: {known})"
        raise ValueError(msg) from None
    return _binding_from_registration(registration)


def detect_output_parser(spec: OutputParserSpec) -> OutputParserBinding:
    """Return the best parser for ``spec``, falling back to generic."""
    if spec.kind is not None and spec.kind in _registry:
        return get_output_parser(spec.kind)
    command = Path(spec.argv[0]).name if spec.argv else ""
    candidates = [
        registration
        for registration in _registry.values()
        if registration.name != "generic"
        and _matches(registration, spec, command)
    ]
    if candidates:
        candidates.sort(key=lambda item: (-item.priority, item.order))
        return _binding_from_registration(candidates[0])
    return get_output_parser("generic")


def registered_output_parsers() -> tuple[str, ...]:
    """Return registered output parser names."""
    return tuple(sorted(_registry))


def binding_for_parser(parser: ProcessOutputParser) -> OutputParserBinding:
    """Return metadata for a directly supplied parser callable."""
    capabilities = getattr(parser, "capabilities", _NO_CAPABILITIES)
    if not isinstance(capabilities, OutputParserCapabilities):
        capabilities = _NO_CAPABILITIES
    return OutputParserBinding(
        name=None,
        parser=parser,
        capabilities=capabilities,
    )


def _binding_from_registration(
    registration: _OutputParserRegistration,
) -> OutputParserBinding:
    return OutputParserBinding(
        name=registration.name,
        parser=registration.factory(),
        capabilities=registration.capabilities,
    )


def _matches(
    registration: _OutputParserRegistration,
    spec: OutputParserSpec,
    command: str,
) -> bool:
    return command in registration.command_names or (
        registration.predicate is not None and registration.predicate(spec)
    )
