# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Rich logging and log tail helpers."""

from __future__ import annotations

from lograil._internal.async_tail import AsyncLogSource, SubprocessLogSource
from lograil._internal.formatter import LograilFormatter, format_log_entry
from lograil._internal.log import (
    EnvFilter,
    LograilHandler,
    StatusHandle,
    StatusLabel,
    configure_logging,
    quiet,
    status,
    status_label,
    update_status,
)
from lograil._internal.process import (
    ProcessGroupResult,
    ProcessSpec,
    run_process_group,
)
from lograil._internal.progress import (
    ProgressUpdate,
    emit as emit_progress,
    emit_line as emit_progress_line,
    format_line as format_progress_line,
    lograil_instrumentation_env,
)
from lograil._internal.remap import DEFAULT_REMAPS, Remap, RemapPipeline
from lograil._internal.tail import (
    LogEntry,
    LogQuery,
    LogSource,
    stream_log_files,
    tail_to_status,
)
from lograil.parsers import (
    OutputParserCapabilities,
    ProcessOutputParser,
    register_output_parser,
)
from lograil.sources.fd import FileDescriptorLogSource

__all__ = [
    "DEFAULT_REMAPS",
    "AsyncLogSource",
    "EnvFilter",
    "FileDescriptorLogSource",
    "LogEntry",
    "LogQuery",
    "LogSource",
    "LograilFormatter",
    "LograilHandler",
    "OutputParserCapabilities",
    "ProcessGroupResult",
    "ProcessOutputParser",
    "ProcessSpec",
    "ProgressUpdate",
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
]
