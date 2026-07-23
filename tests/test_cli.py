# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from io import StringIO
from unittest.mock import patch

import pytest

from lograil._internal import cli, log
from lograil.sources.docker import DockerBuildLogSource
from lograil.sources.fd import FileDescriptorLogSource


def test_cli_runs_docker_build_source_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
    monkeypatch.delenv("LOGRAIL", raising=False)
    stdin = StringIO("plain chatter\n")

    with patch(
        "lograil._internal.cli.run_source_to_status", return_value=0
    ) as run_source:
        result = cli.main(["--source=docker-build"], stdin=stdin)

    assert result == 0
    source = run_source.call_args.args[0]
    assert isinstance(source, DockerBuildLogSource)


def test_cli_defaults_to_fd_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
    monkeypatch.delenv("LOGRAIL", raising=False)
    stdin = StringIO("plain chatter\n")

    with patch(
        "lograil._internal.cli.run_source_to_status", return_value=0
    ) as run_source:
        result = cli.main([], stdin=stdin)

    assert result == 0
    source = run_source.call_args.args[0]
    assert isinstance(source, FileDescriptorLogSource)


def test_cli_configures_output_and_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
    monkeypatch.delenv("LOGRAIL", raising=False)
    with patch("lograil._internal.cli.run_source_to_status", return_value=0):
        result = cli.main([
            "--source=docker-build",
            "--output=fancy",
            "--filter=debug",
        ])

    assert result == 0
    assert log.output_mode() == "fancy"
    assert log._logger.isEnabledFor(10)


def test_cli_unknown_source_returns_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOGRAIL_OUTPUT", raising=False)
    monkeypatch.delenv("LOGRAIL", raising=False)
    stderr = StringIO()

    result = cli.main(["--source=missing"], stderr=stderr)

    assert result == 2
    assert "unknown log source 'missing'" in stderr.getvalue()
