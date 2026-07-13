# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Concurrent process groups and status dashboards."""

from __future__ import annotations

from typing import TypeAlias

import os
from collections.abc import (
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from functools import partial

import anyio
from anyio.abc import TaskGroup
from rich.columns import Columns
from rich.console import (
    Console as RichConsole,
    ConsoleOptions,
    Group,
    RenderableType,
    RenderResult,
)
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from lograil._internal import console, log, progress, remap
from lograil._internal.async_tail import (
    StreamMode,
    SubprocessLogSource,
    SubprocessStartError,
)
from lograil._internal.remap import Remap
from lograil._internal.tail import LogEntry, emit_entry
from lograil.parsers import ProcessOutputParser
from lograil.parsers._base import (
    OutputParserBinding,
    OutputParserSpec,
    binding_for_parser,
    detect_output_parser,
    get_output_parser,
)

_GROUPED_CELL_WIDTH = 30
_GROUP_LABEL_MAX_WIDTH = 12
_PROGRESS_LABEL_MAX_WIDTH = 18
_CANCELLED_EXIT_CODE = 130
ProcessOutputParserSpec: TypeAlias = str | ProcessOutputParser | None


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """Specification for one subprocess in a process group."""

    argv: Sequence[str]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    name: str | None = None
    process: str | None = None
    subject: str | None = None
    category: str | None = None
    stream: StreamMode = "stderr"
    parser: ProcessOutputParserSpec = None
    remaps: Iterable[Remap] | None = None
    kind: str | None = None

    @property
    def label(self) -> str:
        """Process label shown in results and dashboards."""
        if self.process is not None:
            return self.process
        if self.name is not None:
            return self.name
        return " ".join(self.argv)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Result for one subprocess in a process group."""

    spec: ProcessSpec
    exit_code: int
    success: bool
    entries: int
    tail: tuple[LogEntry, ...]
    last_message: str | None


@dataclass(frozen=True, slots=True)
class ProcessGroupResult:
    """Aggregate result for a process group run."""

    processes: tuple[ProcessResult, ...]

    @property
    def success(self) -> bool:
        """Whether every subprocess exited successfully."""
        return all(result.success for result in self.processes)


@dataclass(slots=True)
class _ProcessState:
    spec: ProcessSpec
    parser: OutputParserBinding = field(init=False)
    running: bool = True
    exit_code: int | None = None
    detail: str | None = None
    completed: int | None = None
    total: int | None = None
    entries: int = 0
    tail: list[LogEntry] = field(default_factory=list)
    spinner: Spinner | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0

    def __post_init__(self) -> None:
        self.parser = _parser_binding_for(self.spec)


class _DashboardRenderable:
    """Adapter that re-renders the dashboard on every Live repaint.

    Handing this to an active status's live display lets its auto-refresh
    pick up dashboard state changes without a second Live.
    """

    def __init__(self, dashboard: _ProcessDashboard) -> None:
        self._dashboard = dashboard

    def __rich__(self) -> RenderableType:
        return self._dashboard.render()

    def __rich_console__(
        self, console: RichConsole, options: ConsoleOptions
    ) -> RenderResult:
        _ = console
        yield self._dashboard.render(width=options.max_width)


class _ProcessDashboard:
    """Live Rich renderable for process group status."""

    def __init__(self, states: Sequence[_ProcessState]) -> None:
        self._states = list(states)

    def finish(self, state: _ProcessState, exit_code: int) -> None:
        if state.parser.capabilities.complete_on_success and exit_code == 0:
            if state.total is None:
                state.completed = 1
                state.total = 1
            elif state.completed is None or state.completed < state.total:
                state.completed = state.total
        state.running = False
        state.exit_code = exit_code

    def __rich_console__(
        self, console: RichConsole, options: ConsoleOptions
    ) -> RenderResult:
        _ = console
        yield self.render(width=options.max_width)

    def render(self, *, width: int | None = None) -> RenderableType:
        show_labels = len(self._categories()) > 1
        groups = self._grouped_spinner_table(show_labels=show_labels)
        progress = self._progress_table(show_labels=show_labels, width=width)
        renderables: list[RenderableType] = []
        if groups.row_count:
            renderables.append(groups)
        if progress.row_count:
            if renderables:
                renderables.append(Text())
            renderables.append(progress)
        if not renderables:
            return Text("processes")
        return Group(*renderables)

    def _grouped_spinner_table(self, *, show_labels: bool) -> Table:
        label_width = self._group_label_width()
        table = Table.grid(padding=0)
        if show_labels:
            table.add_column(width=label_width + 1, no_wrap=True)
        table.add_column(ratio=1)
        by_category: dict[str, list[_ProcessState]] = {}
        for state in self._states:
            if _is_progress_state(state):
                continue
            category = state.spec.category or "processes"
            by_category.setdefault(category, []).append(state)
        for index, (category, states) in enumerate(sorted(by_category.items())):
            if index > 0:
                table.add_row(Text(), Text())
            cells: list[RenderableType] = [
                Columns(
                    [_spinner_cell(state) for state in states],
                    equal=False,
                    expand=False,
                    padding=(0, 1, 0, 0),
                    width=_GROUPED_CELL_WIDTH,
                )
            ]
            if show_labels:
                cells.insert(0, _group_label(category, width=label_width))
            table.add_row(*cells)
        return table

    def _progress_table(self, *, show_labels: bool, width: int | None) -> Table:
        label_width = self._group_label_width()
        progress_label_width = self._progress_label_width()
        progress_width = self._progress_body_width(
            show_labels=show_labels,
            label_width=label_width,
            width=width,
        )
        table = Table.grid(padding=0)
        if show_labels:
            table.add_column(width=label_width + 1, no_wrap=True)
        table.add_column(no_wrap=True, overflow="ellipsis")
        previous_category: str | None = None
        for state in self._states:
            if not _is_progress_state(state):
                continue
            category = state.spec.category or "progress"
            if previous_category is not None and category != previous_category:
                table.add_row(*([Text(), Text()] if show_labels else [Text()]))
            body = _progress_renderable(
                state,
                label_width=progress_label_width,
                max_width=progress_width,
            )
            if show_labels:
                label = (
                    _group_label(category, width=label_width)
                    if category != previous_category
                    else Text()
                )
                table.add_row(label, body)
            else:
                table.add_row(body)
            previous_category = category
        return table

    def _categories(self) -> set[str]:
        categories: set[str] = set()
        for state in self._states:
            if _is_progress_state(state):
                categories.add(state.spec.category or "progress")
            else:
                categories.add(state.spec.category or "processes")
        return categories

    def _group_label_width(self) -> int:
        labels = [
            state.spec.category
            or ("progress" if _is_progress_state(state) else "processes")
            for state in self._states
        ]
        width = max((Text(label).cell_len for label in labels), default=0)
        return min(width, _GROUP_LABEL_MAX_WIDTH)

    def _progress_label_width(self) -> int:
        labels = [
            state.spec.subject or state.spec.label
            for state in self._states
            if _is_progress_state(state)
        ]
        width = max((Text(label).cell_len for label in labels), default=0)
        return min(width, _PROGRESS_LABEL_MAX_WIDTH)

    def _progress_body_width(
        self,
        *,
        show_labels: bool,
        label_width: int,
        width: int | None,
    ) -> int:
        label_column = label_width + 1 if show_labels else 0
        render_width = (
            width if width is not None else console.stderr_console.width
        )
        return max(1, render_width - label_column)


def run_process_group(
    specs: Sequence[ProcessSpec],
    *,
    layout: str = "grouped",
    cancel_on_failure: bool = False,
) -> ProcessGroupResult:
    """Run subprocesses concurrently and render their status.

    In fancy output mode the group renders as a live dashboard (spinner,
    per-process progress, exit markers); in plain and json modes each
    output line is emitted as it arrives.  Per-process failures --
    non-zero exits, start errors, or a parser/remap raising -- are
    recorded on that process's :class:`ProcessResult` and never abort
    siblings; with ``cancel_on_failure=True`` the first failure cancels
    the remaining processes instead.  ``layout`` currently accepts only
    ``"grouped"``.
    """
    return anyio.run(
        partial(
            _run_process_group,
            tuple(specs),
            layout=layout,
            cancel_on_failure=cancel_on_failure,
        )
    )


async def _run_process_group(
    specs: tuple[ProcessSpec, ...],
    *,
    layout: str,
    cancel_on_failure: bool,
) -> ProcessGroupResult:
    if layout != "grouped":
        msg = f"unknown process group layout: {layout}"
        raise ValueError(msg)
    states = [_ProcessState(spec=spec) for spec in specs]
    dashboard = _ProcessDashboard(states)
    active_status = (
        log.get_active_status() if log.fancy_output_enabled() else None
    )
    if not log.fancy_output_enabled():
        await _run_all(
            states,
            dashboard,
            None,
            cancel_on_failure=cancel_on_failure,
        )
    elif active_status is not None:
        # An active status already owns a live display on this console;
        # starting a second Live would raise rich.errors.LiveError.  Hand
        # the dashboard to the status's display instead: its auto-refresh
        # re-renders the dashboard as state changes.
        active_status.use_progress(_DashboardRenderable(dashboard))
        try:
            await _run_all(
                states,
                dashboard,
                _noop_refresh,
                cancel_on_failure=cancel_on_failure,
            )
        finally:
            active_status.resume_status()
    else:
        live = Live(
            dashboard,
            console=console.stderr_console,
            refresh_per_second=12,
            transient=False,
        )
        with live:
            await _run_all(
                states,
                dashboard,
                lambda *, force: live.update(dashboard, refresh=force),
                cancel_on_failure=cancel_on_failure,
            )
    return ProcessGroupResult(
        processes=tuple(_result_from_state(state) for state in states)
    )


def _noop_refresh(*, force: bool = False) -> None:
    """Refresh hook for displays that re-render on their own schedule."""
    _ = force


async def _run_all(
    states: Sequence[_ProcessState],
    dashboard: _ProcessDashboard,
    refresh: Callable[..., None] | None,
    *,
    cancel_on_failure: bool,
) -> None:
    async with anyio.create_task_group() as task_group:
        for state in states:
            task_group.start_soon(
                partial(
                    _run_one,
                    state,
                    dashboard,
                    refresh,
                    task_group,
                    cancel_on_failure=cancel_on_failure,
                )
            )


async def _run_one(
    state: _ProcessState,
    dashboard: _ProcessDashboard,
    refresh: Callable[..., None] | None,
    task_group: TaskGroup,
    *,
    cancel_on_failure: bool,
) -> None:
    source = SubprocessLogSource(
        state.spec.argv,
        cwd=state.spec.cwd,
        env=state.spec.env,
        name=state.spec.label,
        subject=state.spec.subject,
        category=state.spec.category,
        stream=_source_stream_for(state.spec),
        kind=state.spec.kind,
    )
    pipeline = remap.RemapPipeline(
        remap.DEFAULT_REMAPS if state.spec.remaps is None else state.spec.remaps
    )
    try:
        async with source.open() as entries:
            async for raw_entry in entries:
                entry = state.parser.parser(raw_entry)
                mapped = pipeline(entry)
                if mapped is None:
                    continue
                previous_detail = state.detail
                _record_entry(state, mapped)
                _emit_entry(mapped, dashboard_active=refresh is not None)
                if refresh is not None:
                    refresh(force=state.detail != previous_detail)
    except anyio.get_cancelled_exc_class():
        exit_code = source.exit_code
        if exit_code is None:
            exit_code = _CANCELLED_EXIT_CODE
        state.detail = "cancelled"
        dashboard.finish(state, exit_code)
        if refresh is not None:
            refresh(force=True)
        raise
    except SubprocessStartError as exc:
        _record_failure(
            state,
            f"failed to start: {exc}",
            dashboard_active=refresh is not None,
        )
        exit_code = None
    except Exception as exc:  # ruff:ignore[blind-except] - per-process pipeline
        # failure (pump error, user parser/remap raising) is recorded as
        # this process's failure instead of cancelling its siblings.
        _record_failure(
            state,
            f"log pipeline failed: {exc}",
            dashboard_active=refresh is not None,
        )
        exit_code = None
    else:
        exit_code = source.exit_code
    if exit_code is None:
        exit_code = 1
    dashboard.finish(state, exit_code)
    if refresh is not None:
        refresh(force=True)
    if cancel_on_failure and exit_code != 0:
        task_group.cancel_scope.cancel()


def _source_stream_for(spec: ProcessSpec) -> StreamMode:
    # The child decides whether to emit progress lines (to stdout) from
    # its effective environment: an explicit spec.env, or the inherited
    # parent environment when spec.env is None.
    env: Mapping[str, str] = spec.env if spec.env is not None else os.environ
    if spec.stream == "stderr" and progress.PROGRESS_LINES_ENV in env:
        return "combined"
    return spec.stream


def _parser_for(spec: ProcessSpec) -> ProcessOutputParser:
    return _parser_binding_for(spec).parser


def _parser_binding_for(spec: ProcessSpec) -> OutputParserBinding:
    parser = spec.parser
    if isinstance(parser, str):
        return get_output_parser(parser)
    if parser is not None:
        return binding_for_parser(parser)
    return detect_output_parser(_output_parser_spec(spec))


def _output_parser_spec(spec: ProcessSpec) -> OutputParserSpec:
    return OutputParserSpec(
        argv=spec.argv,
        cwd=spec.cwd,
        env=spec.env,
        name=spec.name,
        process=spec.process,
        subject=spec.subject,
        category=spec.category,
        stream=spec.stream,
        kind=spec.kind,
    )


def _record_entry(state: _ProcessState, entry: LogEntry) -> None:
    state.entries += 1
    state.tail.append(dict(entry))
    if len(state.tail) > 50:
        del state.tail[:-50]
    if "lograil.status.detail" in entry:
        detail = entry.get("lograil.status.detail")
    else:
        detail = entry.get("message")
    if isinstance(detail, str) and detail:
        state.detail = detail
    total = entry.get(remap.PROGRESS_TOTAL)
    completed = entry.get(remap.PROGRESS_COMPLETED)
    if isinstance(total, int) and isinstance(completed, int):
        state.total = total
        state.completed = completed


def _emit_entry(entry: LogEntry, *, dashboard_active: bool) -> None:
    if dashboard_active:
        return
    emit_entry(entry, show_context=True)


def _record_failure(
    state: _ProcessState, message: str, *, dashboard_active: bool
) -> None:
    """Record and emit a synthetic per-process ERROR entry.

    Without the emit, a process that fails to start would produce no
    output at all in plain and json modes.
    """
    entry: LogEntry = {
        "message": message,
        "name": state.spec.label,
        "levelname": "ERROR",
        "lograil.process": state.spec.label,
    }
    _record_entry(state, entry)
    _emit_entry(entry, dashboard_active=dashboard_active)


def _result_from_state(state: _ProcessState) -> ProcessResult:
    exit_code = state.exit_code if state.exit_code is not None else 1
    return ProcessResult(
        spec=state.spec,
        exit_code=exit_code,
        success=exit_code == 0,
        entries=state.entries,
        tail=tuple(state.tail),
        last_message=state.detail,
    )


def _is_progress_state(state: _ProcessState) -> bool:
    return state.total is not None or state.parser.capabilities.starts_progress


def _group_label(label: str, *, width: int) -> Text:
    text = Text(label, style="bold", no_wrap=True, overflow="ellipsis")
    if text.cell_len > width:
        text.truncate(width, overflow="ellipsis")
    elif text.cell_len < width:
        text.pad_right(width - text.cell_len)
    text.append(" ")
    return text


def _state_spinner(state: _ProcessState, text: Text, *, style: str) -> Spinner:
    """Return the state's persistent spinner with refreshed text.

    Recreating the Spinner on every dashboard render would reset its
    start time each repaint and freeze the animation on its first frame.
    """
    if state.spinner is None:
        state.spinner = Spinner("dots", text=text, style=style)
    else:
        state.spinner.text = text
        state.spinner.style = style
    return state.spinner


def _spinner_cell(state: _ProcessState) -> RenderableType:
    label = _grouped_cell_text(state)
    if state.running:
        return _state_spinner(state, label, style="cyan")
    if state.success:
        return Text.assemble(Text("✓", style="green"), " ", label)
    return Text.assemble(Text("✗", style="red"), " ", label)


def _grouped_cell_text(state: _ProcessState) -> Text:
    text = Text(state.spec.label, no_wrap=True, overflow="ellipsis")
    if state.detail:
        text.append(" ")
        text.append(state.detail, style="dim")
    if text.cell_len > _GROUPED_CELL_WIDTH - 2:
        text.truncate(_GROUPED_CELL_WIDTH - 2, overflow="ellipsis")
    return text


def _progress_renderable(
    state: _ProcessState,
    *,
    label_width: int,
    max_width: int,
) -> RenderableType:
    body = _progress_body(
        state,
        label_width=label_width,
        max_width=max_width - 2,
    )
    if state.running:
        return _state_spinner(state, body, style="status.spinner")
    return Text.assemble(_done_marker(state), " ", body)


def _progress_values(state: _ProcessState) -> tuple[int, int]:
    if state.total is None:
        return 0, 100
    return state.completed or 0, state.total


def _done_marker(state: _ProcessState) -> Text:
    if state.success:
        return Text("✓", style="green")
    return Text("✗", style="red")


def _progress_body(
    state: _ProcessState, *, label_width: int, max_width: int
) -> Text:
    label = _fixed_text(state.spec.subject or state.spec.label, label_width)
    completed, total = _progress_values(state)
    return _progress_body_with_bar(
        label,
        completed=completed,
        total=total,
        detail=_progress_detail(state),
        max_width=max_width,
    )


def _progress_detail(state: _ProcessState) -> str:
    if state.detail:
        return state.detail
    if state.running and state.parser.capabilities.starts_progress:
        if state.parser.name:
            return f"starting {state.parser.name}"
        return "starting"
    return ""


def _progress_body_with_bar(
    label: Text,
    *,
    completed: int,
    total: int,
    detail: str,
    max_width: int,
) -> Text:
    pct = progress.progress_percent(completed=completed, total=total)
    bar = progress.render_progress_bar(completed=completed, total=total)
    prefix = Text.assemble(
        label,
        " ",
        bar,
        " ",
        Text(f"{pct:>3d}%", style="progress.percentage"),
    )
    if pct >= 100:
        prefix.no_wrap = True
        prefix.overflow = "ellipsis"
        return prefix
    detail_width = max(0, max_width - prefix.cell_len - 1)
    text = prefix
    if detail_width:
        text.append(" ")
        detail_text = _fixed_text(
            _single_line_detail(detail),
            detail_width,
            pad=False,
        )
        detail_text.stylize("dim")
        text.append_text(detail_text)
    text.no_wrap = True
    text.overflow = "ellipsis"
    return text


def _fixed_text(value: str, width: int, *, pad: bool = True) -> Text:
    text = Text(value, no_wrap=True, overflow="ellipsis")
    if text.cell_len > width:
        text.truncate(width, overflow="ellipsis")
    elif pad and text.cell_len < width:
        text.pad_right(width - text.cell_len)
    return text


def _single_line_detail(value: str) -> str:
    return " ".join(value.splitlines())
