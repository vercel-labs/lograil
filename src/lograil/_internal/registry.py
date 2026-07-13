# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Shared source-registry machinery for LogSource hierarchies."""

from __future__ import annotations

from typing import Any, ClassVar


class SourceRegistryBase:
    """Mixin providing ``source_id`` registration for a source hierarchy.

    Each registry root must declare its own ``_registry`` class attribute
    (an empty dict) and may override ``_registry_label`` for error messages.
    Subclasses register by passing ``source_id`` as a class keyword::

        class FileLogSource(LogSource, source_id="file"): ...
    """

    _registry: ClassVar[dict[str, type[Any]]]
    _registry_label: ClassVar[str] = "log source"
    source_id: ClassVar[str | None] = None

    def __init_subclass__(
        cls, *, source_id: str | None = None, **kwargs: Any
    ) -> None:
        super().__init_subclass__(**kwargs)
        if source_id is None:
            return
        cls.source_id = source_id
        cls._registry[source_id] = cls

    @classmethod
    def get(cls, source_id: str) -> type[Any]:
        """Return the registered source class for ``source_id``."""
        try:
            return cls._registry[source_id]
        except KeyError:
            known = ", ".join(sorted(cls._registry)) or "none"
            label = cls._registry_label
            msg = f"unknown {label} '{source_id}' (known: {known})"
            raise ValueError(msg) from None

    @classmethod
    def registered_sources(cls) -> tuple[str, ...]:
        """Return known source identifiers."""
        return tuple(sorted(cls._registry))
