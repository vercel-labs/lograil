# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Command line interface for lograil."""

from __future__ import annotations

from typing import TextIO

import argparse
import contextlib
import importlib
import os
import sys

from lograil._internal import log
from lograil._internal.formatter import OutputMode
from lograil._internal.tail import LogSource, run_source_to_status

_OUTPUT_CHOICES: tuple[OutputMode, ...] = ("plain", "json", "fancy")
_SOURCE_MODULES = ("docker", "fd", "file", "victoria")


def _load_sources() -> None:
    """Import bundled source modules so they self-register.

    Sources whose optional dependencies (extras) are not installed are
    skipped and simply do not appear in the registry.
    """
    for name in _SOURCE_MODULES:
        # Only a missing optional dependency is skippable; any other
        # ImportError is a genuinely broken module and must surface
        # instead of masquerading as an unknown source.
        with contextlib.suppress(ModuleNotFoundError):
            importlib.import_module(f"lograil.sources.{name}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lograil",
        description="Render structured log streams from stdin.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="source identifier, such as docker-build",
    )
    parser.add_argument(
        "--output",
        choices=_OUTPUT_CHOICES,
        help="output mode: plain, json, or fancy",
    )
    parser.add_argument(
        "--filter",
        dest="filter_config",
        help="EnvFilter config using the same syntax as LOGRAIL",
    )
    return parser


def _configure(*, output: OutputMode | None, filter_config: str | None) -> None:
    if output is not None:
        os.environ[log.OUTPUT_ENV] = output
    if filter_config is not None:
        os.environ[log.LOG_ENV] = filter_config
    log.configure_logging()


def main(
    argv: list[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the lograil CLI."""
    args = _parser().parse_args(argv)
    _configure(output=args.output, filter_config=args.filter_config)
    _load_sources()
    try:
        source_cls = LogSource.get(args.source)
        source = source_cls.from_stdin(stdin or sys.stdin)
    except (ValueError, NotImplementedError) as exc:
        err = stderr or sys.stderr
        err.write(f"lograil: {exc}\n")
        return 2
    try:
        return run_source_to_status(source)
    except KeyboardInterrupt:
        # Conventional exit code for SIGINT; no traceback on Ctrl-C.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
