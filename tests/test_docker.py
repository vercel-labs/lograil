# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterable

from lograil._internal.tail import LogEntry
from lograil.sources import docker
from lograil.sources.docker import (
    DockerBuildLogSource,
    DockerLogSource,
    _BuildxJsonProgress,
    _DockerBuildProgressBase,
    docker_logs_to_entries,
)


def _collect_source(
    source: DockerBuildLogSource | DockerLogSource,
    *,
    stop: threading.Event | None = None,
) -> list[LogEntry]:
    stop = threading.Event() if stop is None else stop
    with source.open(stop=stop) as entries:
        return list(entries)


def _make_vertex_msg(
    digest: str,
    name: str,
    *,
    started: bool = False,
    completed: bool = False,
    cached: bool = False,
    error: str | None = None,
) -> str:
    vtx: dict[str, object] = {"digest": digest, "name": name}
    if started:
        vtx["started"] = "2024-01-01T00:00:00Z"
    if completed:
        vtx["completed"] = "2024-01-01T00:00:01Z"
    if cached:
        vtx["cached"] = True
    if error is not None:
        vtx["error"] = error
    return json.dumps({"vertexes": [vtx]})


def _make_log_msg(vertex: str, data: str) -> str:
    encoded = base64.b64encode(data.encode()).decode()
    return json.dumps({
        "logs": [{"vertex": vertex, "data": encoded, "stream": 1}]
    })


def test_plain_stage_progress_and_cache() -> None:
    source = DockerBuildLogSource([
        "#5 [base 1/3] FROM alpine:3.19\n",
        "#5 CACHED\n",
    ])

    entries = _collect_source(source)

    assert entries[0]["lograil.progress.subject"] == "base"
    assert entries[0]["lograil.progress.total"] == 3
    assert entries[1]["message"] == "#5 CACHED"
    assert entries[1]["lograil.status_only"] is True


def test_plain_error_line_passed_through() -> None:
    source = DockerBuildLogSource([
        "#5 [base 1/3] FROM alpine:3.19\n",
        "#5 ERROR: failed to solve\n",
    ])

    entries = _collect_source(source)

    assert entries[1]["levelname"] == "ERROR"
    assert "ERROR" in entries[1]["message"]


def test_rawjson_vertex_progression_and_logs() -> None:
    progress = _BuildxJsonProgress(image_name="test")
    digest = "sha256:abc123"

    progress.process_line(
        _make_vertex_msg(digest, "[1/3] FROM alpine", started=True)
    )
    progress.process_line(_make_log_msg(digest, "compiling main.c"))
    progress.process_line(
        _make_vertex_msg(
            digest, "[1/3] FROM alpine", completed=True, cached=True
        )
    )

    assert progress._vertices[digest].started is True
    assert progress._vertices[digest].completed is True
    assert progress._vertices[digest].cached is True
    assert progress._get_current() == 1
    assert "compiling main.c" in progress.get_captured_output()


def test_rawjson_error_captured() -> None:
    progress = _BuildxJsonProgress(image_name="test")

    progress.process_line(
        _make_vertex_msg(
            "sha256:a",
            "[2/3] RUN make",
            started=True,
            completed=True,
            error="process exited with code 1",
        )
    )

    assert "ERROR: process exited with code 1" in progress.get_captured_output()


def test_rawjson_progress_does_not_own_live_outside_fancy_mode() -> None:
    progress = _BuildxJsonProgress(image_name="test")

    progress.process_line(
        _make_vertex_msg("sha256:abc", "[1/3] FROM alpine", started=True)
    )

    assert progress._owns_live is False
    assert progress._progress is not None
    assert progress._progress.live.is_started is False


def test_rawjson_batched_vertexes_all_emitted() -> None:
    line = json.dumps({
        "vertexes": [
            {
                "digest": "sha256:err",
                "name": "[1/2] RUN make",
                "error": "process exited with code 1",
            },
            {
                "digest": "sha256:ok",
                "name": "[2/2] COPY . .",
                "started": "2024-01-01T00:00:00Z",
            },
        ]
    })
    source = DockerBuildLogSource([f"{line}\n"])

    entries = _collect_source(source)

    assert len(entries) == 2
    assert entries[0]["levelname"] == "ERROR"
    assert "process exited with code 1" in str(entries[0]["message"])
    assert entries[1]["message"] == "COPY . ."
    assert entries[1]["lograil.progress.total"] == 2


def test_rawjson_multiline_log_record_emits_every_line() -> None:
    vertex = _make_vertex_msg("sha256:abc", "[1/2] RUN make", started=True)
    output = _make_log_msg(
        "sha256:abc", "compiling a.c\nerror: b.c:1: oops\ncompiling c.c\n"
    )
    source = DockerBuildLogSource([f"{vertex}\n", f"{output}\n"])

    entries = _collect_source(source)

    assert [entry["message"] for entry in entries[1:]] == [
        "compiling a.c",
        "error: b.c:1: oops",
        "compiling c.c",
    ]


def test_docker_log_source_reads_lines() -> None:
    source = DockerLogSource([b"one\ntwo\n"])

    entries = _collect_source(source)

    assert entries == [
        {"message": "one", "name": "docker"},
        {"message": "two", "name": "docker"},
    ]


def test_docker_log_source_yields_complete_lines_before_iterator_end() -> None:
    def chunks() -> Iterable[bytes]:
        yield b"one\n"
        assert yielded.is_set()
        yield b"two\n"

    yielded = threading.Event()
    entries = docker_logs_to_entries(chunks())

    assert next(iter(entries)) == {"message": "one", "name": "docker"}
    yielded.set()


def test_docker_log_source_crlf_across_chunks_has_no_phantom_line() -> None:
    source = DockerLogSource([b"hello\r", b"\nworld\n"])

    entries = _collect_source(source)

    assert entries == [
        {"message": "hello", "name": "docker"},
        {"message": "world", "name": "docker"},
    ]


def test_docker_log_source_flushes_trailing_partial_line() -> None:
    entries = list(docker_logs_to_entries([b"one\ntail"]))

    assert entries == [
        {"message": "one", "name": "docker"},
        {"message": "tail", "name": "docker"},
    ]


def test_docker_log_source_splits_carriage_return_progress() -> None:
    entries = _collect_source(DockerLogSource([b"step 1\rstep 2\r"]))

    assert entries == [
        {"message": "step 1", "name": "docker"},
        {"message": "step 2", "name": "docker"},
    ]


def test_docker_build_source_processes_plain_progress() -> None:
    source = DockerBuildLogSource(["#5 [base 1/3] FROM alpine:3.19\n"])

    entries = _collect_source(source)

    assert entries[0]["message"] == "FROM alpine:3.19"
    assert entries[0]["lograil.progress.completed"] == 0
    assert entries[0]["lograil.progress.total"] == 3
    assert entries[0]["lograil.progress.process"] == "docker-build"
    assert entries[0]["lograil.progress.subject"] == "base"


def test_docker_build_source_processes_rawjson_progress() -> None:
    line = _make_vertex_msg("sha256:abc", "[1/2] FROM alpine", started=True)
    source = DockerBuildLogSource([f"{line}\n"])

    entries = _collect_source(source)

    assert entries[0]["message"] == "FROM alpine"
    assert entries[0]["lograil.progress.completed"] == 0
    assert entries[0]["lograil.progress.total"] == 2
    assert entries[0]["lograil.progress.process"] == "docker-build"


def test_docker_build_source_keeps_rawjson_logs_in_progress() -> None:
    vertex = _make_vertex_msg("sha256:abc", "[1/2] RUN make", started=True)
    output = _make_log_msg("sha256:abc", "step 1: compiling\n")
    source = DockerBuildLogSource([f"{vertex}\n", f"{output}\n"])

    entries = _collect_source(source)

    assert entries[1]["message"] == "step 1: compiling"
    assert entries[1]["lograil.progress.completed"] == 0
    assert entries[1]["lograil.progress.total"] == 2
    assert entries[1]["lograil.progress.process"] == "docker-build"


def test_docker_build_source_updates_status_for_chatter() -> None:
    source = DockerBuildLogSource(["loading build context\n"])

    entries = _collect_source(source)

    assert entries == [
        {
            "message": "loading build context",
            "name": "docker-build",
            "lograil.status_only": True,
        }
    ]


def test_removed_plain_build_handler_is_not_exported() -> None:
    assert "create_docker_build_handler" not in docker.__all__
    assert not hasattr(docker, "create_docker_build_handler")


def test_docker_progress_accepts_bracketed_descriptions() -> None:
    progress = _DockerBuildProgressBase(image_name="image[/tag]")

    progress._update_progress("[internal] load [/some/path]", 0, 1)
    progress.finish(success=True)
