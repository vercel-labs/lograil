# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Docker progress helpers for lograil."""

from __future__ import annotations

from typing import TextIO, cast

import base64
import json
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from rich.markup import escape as _escape_markup
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
)
from rich.text import Text

from lograil._internal import console, log, remap
from lograil._internal.lines import flush_remainder, split_byte_lines
from lograil._internal.tail import LogEntry, LogQuery, LogSource

__all__ = [
    "BuildProgress",
    "DockerBuildLogSource",
    "DockerLogSource",
    "create_buildx_json_handler",
    "docker_logs_to_entries",
]


class _PaddedMofNColumn(ProgressColumn):
    """Progress column showing fixed-width M/N."""

    def render(self, task: Task) -> Text:
        completed = int(task.completed)
        if task.total is None:
            return Text(f"{completed:2d}/??", style="progress.percentage")
        total = int(task.total)
        return Text(f"{completed:2d}/{total:<2d}", style="progress.percentage")


_STEP_START_PATTERN = re.compile(r"^#(\d+)\s+\[([^\]]+)\]\s*(.*)?$")
_STEP_START_PATTERN_ALT = re.compile(r"^#(\d+)\s{2,}(\S+.*)$")
_STEP_DONE_PATTERN = re.compile(r"^#(\d+)\s+DONE\s+([\d.]+)s?$")
_STEP_CACHED_PATTERN = re.compile(r"^#(\d+)\s+CACHED$")
_STEP_ERROR_PATTERN = re.compile(r"^#(\d+)\s+ERROR[:\s](.*)$")
_STEP_PROGRESS_PATTERN = re.compile(
    r"(?:(\S+)\s+)?(?:stage-\d+\s+)?(\d+)/(\d+)"
)
_STAGE_PREFIX_PATTERN = re.compile(r"^(?:\S+\s+)?\d+/\d+\s*")
_EXPORTING_PATTERN = re.compile(r"^#(\d+)\s+exporting to", re.IGNORECASE)
_NEW_BUILD_PATTERN = re.compile(r"^#0\s+building with")
_STEP_OUTPUT_PATTERN = re.compile(r"^#(\d+)\s+[\d.]+\s+(.+)$")


@dataclass
class _DockerBuildProgressBase:
    """Base Rich progress display shared by Docker parsers."""

    image_name: str = "image"
    _captured_output: list[str] = field(default_factory=list)
    _progress: Progress | None = field(default=None, repr=False)
    _task_id: TaskID | None = field(default=None, repr=False)
    _started: bool = field(default=False, repr=False)
    _owns_live: bool = field(default=False, repr=False)

    def _get_description_width(self) -> int:
        terminal_width = console.stderr_console.width
        fixed_width = 2 + 5 + 20 + 5
        image_name_width = Text(self.image_name).cell_len
        return max(terminal_width - fixed_width - image_name_width, 30)

    def _truncate(self, text: str, max_len: int | None = None) -> str:
        max_len = self._get_description_width() if max_len is None else max_len
        if len(text) <= max_len:
            return text
        return text[: max_len - 3] + "..."

    def _ensure_progress(self) -> None:
        if self._progress is None:
            image_name = self.image_name
            prefix = log.get_sticky_prefix()
            if prefix is not None:
                image_name = f"{prefix}: {image_name}"
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn(_escape_markup(image_name)),
                BarColumn(bar_width=20),
                _PaddedMofNColumn(),
                TextColumn(
                    "{task.description}",
                    style="dim",
                    markup=False,
                ),
                transient=True,
                console=console.stderr_console,
            )
        if not self._started:
            self._task_id = self._progress.add_task(
                self._truncate("Starting..."), total=None
            )
            active = log.get_active_status()
            if active is not None:
                active.use_progress(self._progress)
            elif log.fancy_output_enabled():
                self._progress.start()
                self._owns_live = True
            # Plain and json modes never start an owned live display: its
            # background refresh thread would keep painting ANSI frames
            # over passthrough lines or NDJSON on stderr.
            self._started = True

    def _update_progress(
        self, description: str, current: int | None, total: int | None
    ) -> None:
        self._ensure_progress()
        if self._progress is not None and self._task_id is not None:
            self._progress.update(
                self._task_id,
                description=self._truncate(description),
                completed=current or 0,
                total=total,
            )

    def _stop_progress(self) -> None:
        if self._progress and self._started:
            if self._owns_live:
                self._progress.stop()
            self._started = False

    def finish(self, *, success: bool) -> None:
        """Stop progress display and resume any parent spinner."""
        _ = success
        self._stop_progress()
        active = log.get_active_status()
        if active is not None:
            active.resume_status()

    def get_captured_output(self) -> str:
        """Return all captured build output."""
        return "\n".join(self._captured_output)


@dataclass
class _PlainBuildProgress:
    """Passthrough handler for plain output mode."""

    _captured_output: list[str] = field(default_factory=list)

    def process_line(self, line: str) -> str | None:
        """Capture and return non-empty output lines."""
        line = line.rstrip("\r\n")
        self._captured_output.append(line)
        if line and not line.isspace():
            return line
        return None

    def finish(self, *, success: bool) -> None:
        """No-op finish for plain output mode."""
        _ = success

    def get_captured_output(self) -> str:
        """Return all captured output."""
        return "\n".join(self._captured_output)


_DOCKER_BUILD_STEP_PATTERN = re.compile(
    r"^\[(?:(\S+)\s+)?(\d+)/(\d+)\]\s+(.*)$"
)


def _decode_rawjson_log_data(raw_data: object) -> str | None:
    """Decode the base64 payload of a buildx rawjson log record.

    Returns the stripped text, or None when the payload is missing,
    malformed, or empty.
    """
    if not isinstance(raw_data, str):
        return None
    try:
        decoded = (
            base64.b64decode(raw_data).decode("utf-8", errors="replace").strip()
        )
    except (ValueError, UnicodeError):
        return None
    return decoded or None


@dataclass
class _Vertex:
    digest: str
    name: str
    started: bool = False
    completed: bool = False
    cached: bool = False
    error: str | None = None
    stage_name: str | None = None
    step_current: int | None = None
    step_total: int | None = None


@dataclass
class _BuildxJsonProgress(_DockerBuildProgressBase):
    """Tracks progress from buildx ``--progress=rawjson`` output."""

    _vertices: dict[str, _Vertex] = field(default_factory=dict)
    _stage_totals: dict[str, int] = field(default_factory=dict)
    _completed_step_keys: set[tuple[str, int]] = field(default_factory=set)

    def process_line(self, line: str) -> str | None:
        """Process one buildx raw JSON progress line."""
        line = line.rstrip("\r\n")
        self._captured_output.append(line)
        if not line or line.isspace():
            return None
        try:
            msg: dict[str, object] = json.loads(line)
        except json.JSONDecodeError:
            return None
        vertexes = msg.get("vertexes")
        if isinstance(vertexes, list):
            for data in cast("list[dict[str, object]]", vertexes):
                self._process_vertex(data)
        logs = msg.get("logs")
        if isinstance(logs, list):
            for data in cast("list[dict[str, object]]", logs):
                self._process_log(data)
        return None

    def _get_total(self) -> int:
        return sum(self._stage_totals.values()) + 1

    def _get_current(self) -> int:
        return len(self._completed_step_keys)

    def _process_vertex(self, data: dict[str, object]) -> None:
        digest = data.get("digest")
        name = data.get("name")
        if not isinstance(digest, str) or not isinstance(name, str):
            return
        match = _DOCKER_BUILD_STEP_PATTERN.match(name)
        if match is None:
            total = sum(self._stage_totals.values())
            if total > 0 and self._get_current() >= total:
                self._update_progress(
                    name, self._get_current(), self._get_total()
                )
            return
        stage_name = match.group(1)
        step_current = int(match.group(2))
        step_total = int(match.group(3))
        description = match.group(4)
        vtx = self._vertices.get(digest)
        if vtx is None:
            vtx = _Vertex(digest=digest, name=name)
            self._vertices[digest] = vtx
        vtx.stage_name = stage_name
        vtx.step_current = step_current
        vtx.step_total = step_total
        effective_stage = stage_name or ""
        if (
            effective_stage not in self._stage_totals
            or step_total > self._stage_totals[effective_stage]
        ):
            self._stage_totals[effective_stage] = step_total
        vtx.started = vtx.started or data.get("started") is not None
        vtx.completed = vtx.completed or data.get("completed") is not None
        vtx.cached = vtx.cached or bool(data.get("cached"))
        if vtx.completed:
            self._completed_step_keys.add((effective_stage, step_current))
        error = data.get("error")
        if isinstance(error, str):
            vtx.error = error
            self._captured_output.append(f"ERROR: {error}")
        desc = f"{stage_name} {description}" if stage_name else description
        if vtx.cached and vtx.completed:
            desc += " (cached)"
        self._update_progress(desc, self._get_current(), self._get_total())

    def _process_log(self, data: dict[str, object]) -> None:
        vertex_digest = data.get("vertex")
        if not isinstance(vertex_digest, str):
            return
        decoded = _decode_rawjson_log_data(data.get("data"))
        if decoded is None:
            return
        self._captured_output.append(decoded)
        vtx = self._vertices.get(vertex_digest)
        if vtx is not None and vtx.step_current is not None:
            last_line = decoded.rsplit("\n", maxsplit=1)[-1].strip()
            if last_line:
                self._update_progress(
                    last_line, self._get_current(), self._get_total()
                )


BuildProgress = _BuildxJsonProgress | _PlainBuildProgress


def create_buildx_json_handler(
    image_name: str = "image",
) -> tuple[BuildProgress, Callable[[str], str | None], str]:
    """Create a buildx rawjson progress handler and progress flag."""
    if not log.fancy_output_enabled():
        # Only fancy mode may drive a Rich live display; plain mode wants
        # passthrough lines and json mode writes NDJSON to stderr that a
        # progress bar would corrupt.
        progress: BuildProgress = _PlainBuildProgress()
        return progress, progress.process_line, "--progress=plain"
    progress = _BuildxJsonProgress(image_name=image_name)
    return progress, progress.process_line, "--progress=rawjson"


@dataclass
class _DockerBuildStepState:
    description: str
    completed: int
    total: int
    subject: str | None = None


class DockerBuildLogSource(LogSource, source_id="docker-build"):
    """Adapt Docker build output to progress-aware log entries."""

    def __init__(
        self, lines: Iterable[str], *, image_name: str = "image"
    ) -> None:
        """Initialize with Docker build log lines."""
        self._lines = lines
        self._image_name = image_name
        self._plain_steps: dict[int, _DockerBuildStepState] = {}
        self._current_plain_step: int | None = None
        self._rawjson_vertices: dict[str, _DockerBuildStepState] = {}

    @classmethod
    def from_stdin(cls, stdin: TextIO) -> DockerBuildLogSource:
        """Create a Docker build log source from stdin."""
        return cls(stdin)

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        """Open Docker build lines as a progress-aware entry handle."""
        _ = query
        yield self._read_entries(stop=stop)

    def _read_entries(self, *, stop: threading.Event) -> Iterator[LogEntry]:
        for raw_line in self._lines:
            if stop.is_set():
                break
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            yield from self._entries(line)

    def _entries(self, line: str) -> Iterator[LogEntry]:
        progress_entry = self._plain_progress_entry(line)
        if progress_entry is not None:
            yield progress_entry
            return
        rawjson_entries = self._rawjson_progress_entries(line)
        if rawjson_entries is not None:
            yield from rawjson_entries
            return
        yield _status_entry(line)

    def _plain_progress_entry(self, line: str) -> LogEntry | None:
        match = _STEP_START_PATTERN.match(line)
        if match is not None:
            step_num = int(match.group(1))
            bracket_content = match.group(2)
            rest = match.group(3) or ""
            platform, current, total, clean_desc = _parse_plain_step_info(
                bracket_content
            )
            if current is None or total is None:
                # Keep the step text after the bracket: "#1 [internal] load
                # build definition" should read "internal load build
                # definition", not just "internal".
                description = (
                    f"{clean_desc} {rest}".strip() if rest else clean_desc
                )
                return _status_entry(description or line)
            stage_name = _plain_stage_name(bracket_content)
            description = f"{clean_desc} {rest}".strip() if rest else clean_desc
            state = _DockerBuildStepState(
                description=description or line,
                completed=max(0, current - 1),
                total=total,
                subject=stage_name or platform,
            )
            self._plain_steps[step_num] = state
            self._current_plain_step = step_num
            return _progress_entry(
                description=state.description,
                completed=state.completed,
                total=state.total,
                process="docker-build",
                subject=state.subject,
            )
        match = _STEP_START_PATTERN_ALT.match(line)
        if match is not None:
            self._current_plain_step = int(match.group(1))
            return _status_entry(match.group(2))
        match = _STEP_DONE_PATTERN.match(line)
        if match is not None:
            step_num = int(match.group(1))
            done_state = self._plain_steps.get(step_num)
            if done_state is not None:
                done_state.completed = done_state.total
            return _status_entry(line)
        match = _STEP_CACHED_PATTERN.match(line)
        if match is not None:
            step_num = int(match.group(1))
            cached_state = self._plain_steps.get(step_num)
            if cached_state is not None:
                cached_state.completed = cached_state.total
            return _status_entry(line)
        match = _STEP_ERROR_PATTERN.match(line)
        if match is not None:
            return {
                "message": line,
                "name": "docker-build",
                "levelname": "ERROR",
            }
        match = _STEP_OUTPUT_PATTERN.match(line)
        if match is not None:
            output = match.group(2).strip()
            output_state = self._plain_steps.get(int(match.group(1)))
            if output_state is not None:
                return _progress_entry(
                    description=output,
                    completed=output_state.completed,
                    total=output_state.total,
                    process="docker-build",
                    subject=output_state.subject,
                )
            return _status_entry(output)
        if _NEW_BUILD_PATTERN.match(line) or _EXPORTING_PATTERN.match(line):
            return _status_entry(line)
        return None

    def _rawjson_progress_entries(self, line: str) -> list[LogEntry] | None:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        # A single rawjson message routinely batches several vertex
        # updates and log records; every one of them must be emitted --
        # skipping any could swallow an ERROR vertex or build output.
        entries: list[LogEntry] = []
        vertexes = data.get("vertexes")
        if isinstance(vertexes, list):
            for item in vertexes:
                if not isinstance(item, dict):
                    continue
                entry = self._rawjson_vertex_entry(
                    cast("dict[str, object]", item)
                )
                if entry is not None:
                    entries.append(entry)
        logs = data.get("logs")
        if isinstance(logs, list):
            for item in logs:
                if not isinstance(item, dict):
                    continue
                entries.extend(
                    self._rawjson_log_entries(cast("dict[str, object]", item))
                )
        if entries:
            return entries
        looks_like = isinstance(vertexes, list) or isinstance(logs, list)
        return [_status_entry(line)] if looks_like else None

    def _rawjson_vertex_entry(self, data: dict[str, object]) -> LogEntry | None:
        digest = data.get("digest")
        name = data.get("name")
        if not isinstance(name, str):
            return None
        error = data.get("error")
        if isinstance(error, str):
            return {
                "message": f"ERROR: {error}",
                "name": "docker-build",
                "levelname": "ERROR",
            }
        match = _DOCKER_BUILD_STEP_PATTERN.match(name)
        if match is None:
            return _status_entry(name)
        stage_name = match.group(1)
        current = int(match.group(2))
        total = int(match.group(3))
        description = match.group(4)
        completed = (
            current if data.get("completed") is not None else current - 1
        )
        desc = f"{stage_name} {description}" if stage_name else description
        if data.get("cached") and data.get("completed") is not None:
            desc += " (cached)"
        state = _DockerBuildStepState(
            description=desc,
            completed=max(0, completed),
            total=total,
            subject=stage_name,
        )
        if isinstance(digest, str):
            self._rawjson_vertices[digest] = state
        return _progress_entry(
            description=state.description,
            completed=state.completed,
            total=state.total,
            process="docker-build",
            subject=state.subject,
        )

    def _rawjson_log_entries(self, data: dict[str, object]) -> list[LogEntry]:
        decoded = _decode_rawjson_log_data(data.get("data"))
        if decoded is None:
            return []
        vertex = data.get("vertex")
        state = (
            self._rawjson_vertices.get(vertex)
            if isinstance(vertex, str)
            else None
        )
        # One log record can carry many lines; emit them all -- the build
        # output (e.g. a compiler error) may be anywhere in the payload.
        entries: list[LogEntry] = []
        for raw_message in decoded.splitlines():
            message = raw_message.strip()
            if not message:
                continue
            if state is None:
                entries.append(_status_entry(message))
            else:
                entries.append(
                    _progress_entry(
                        description=message,
                        completed=state.completed,
                        total=state.total,
                        process="docker-build",
                        subject=state.subject,
                    )
                )
        return entries


_STEP_COUNTER_PATTERN = re.compile(r"\d+/\d+")


def _plain_stage_name(bracket_content: str) -> str | None:
    """Return the stage name from step bracket content, if present.

    Bracket content looks like ``2/7``, ``builder 2/7``, or
    ``linux/amd64 builder 2/7``; neither the platform nor the step
    counter itself is a stage name.
    """
    tokens = bracket_content.split()
    if tokens and tokens[0].startswith("linux/"):
        tokens = tokens[1:]
    if not tokens or _STEP_COUNTER_PATTERN.fullmatch(tokens[0]):
        return None
    return tokens[0]


def _parse_plain_step_info(
    description: str,
) -> tuple[str | None, int | None, int | None, str]:
    platform = None
    current = None
    total = None
    if description.startswith("linux/"):
        parts = description.split(" ", 1)
        platform = parts[0]
        description = parts[1] if len(parts) > 1 else ""
    match = _STEP_PROGRESS_PATTERN.search(description)
    if match:
        if match.group(1) and match.group(1).startswith("linux/"):
            platform = match.group(1)
        current = int(match.group(2))
        total = int(match.group(3))
    clean_desc = _STAGE_PREFIX_PATTERN.sub("", description).strip()
    return platform, current, total, clean_desc


def _progress_entry(
    *,
    description: str,
    completed: int,
    total: int,
    process: str,
    subject: str | None,
) -> LogEntry:
    entry: LogEntry = {
        "message": description,
        "name": "docker-build",
        remap.PROGRESS_DESCRIPTION: description,
        remap.PROGRESS_COMPLETED: completed,
        remap.PROGRESS_TOTAL: total,
        remap.PROGRESS_PROCESS: process,
    }
    if subject is not None:
        entry[remap.PROGRESS_SUBJECT] = subject
    return entry


def _status_entry(message: str) -> LogEntry:
    return {
        "message": message,
        "name": "docker-build",
        remap.STATUS_ONLY: True,
    }


class DockerLogSource(LogSource):
    """Adapt Docker log line streams to the generic LogSource protocol."""

    def __init__(self, lines: Iterable[str | bytes]) -> None:
        """Initialize with Docker SDK log chunks."""
        self._lines = lines

    @contextmanager
    def open(
        self, *, stop: threading.Event, query: LogQuery | None = None
    ) -> Iterator[Iterator[LogEntry]]:
        """Open Docker log chunks as a generic entry handle."""
        _ = query
        yield self._read_entries(stop=stop)

    def _read_entries(self, *, stop: threading.Event) -> Iterator[LogEntry]:
        for entry in docker_logs_to_entries(self._lines):
            if stop.is_set():
                break
            yield entry


def docker_logs_to_entries(lines: Iterable[str | bytes]) -> Iterable[LogEntry]:
    """Convert Docker SDK log chunks to generic log entries."""
    buffer = b""
    for raw in lines:
        chunk = raw if isinstance(raw, bytes) else raw.encode()
        raw_lines, buffer = split_byte_lines(buffer, chunk)
        for raw_line in raw_lines:
            line = raw_line.decode(errors="replace")
            if line.strip():
                yield {"message": line, "name": "docker"}
    for raw_line in flush_remainder(buffer):
        line = raw_line.decode(errors="replace")
        if line.strip():
            yield {"message": line, "name": "docker"}
