# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

import lograil
from lograil.sources.docker import DockerBuildLogSource


def test_top_level_all_is_curated_public_surface() -> None:
    assert set(lograil.__all__) == {
        "AsyncLogSource",
        "EnvFilter",
        "FileDescriptorLogSource",
        "LograilFormatter",
        "LograilHandler",
        "LogEntry",
        "LogQuery",
        "LogSource",
        "ProcessGroupResult",
        "ProcessOutputParser",
        "ProcessSpec",
        "ProgressUpdate",
        "OutputParserCapabilities",
        "DEFAULT_REMAPS",
        "Remap",
        "RemapPipeline",
        "StatusHandle",
        "StatusLabel",
        "SubprocessLogSource",
        "configure_logging",
        "emit_progress",
        "emit_progress_line",
        "format_log_entry",
        "format_progress_line",
        "lograil_instrumentation_env",
        "quiet",
        "register_output_parser",
        "run_process_group",
        "status",
        "status_label",
        "stream_log_files",
        "tail_to_status",
        "update_status",
    }


def test_log_source_registry_tracks_builtins() -> None:
    assert lograil.LogSource.get("docker-build") is DockerBuildLogSource
    assert "docker-build" in lograil.LogSource.registered_sources()


def test_log_source_registry_rejects_unknown_source() -> None:
    with pytest.raises(ValueError, match="unknown log source 'missing'"):
        lograil.LogSource.get("missing")
