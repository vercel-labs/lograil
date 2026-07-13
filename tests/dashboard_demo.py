# SPDX-FileCopyrightText: 2026 Vercel, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Interactive process-group dashboard demo.

Run with:
    LOGRAIL_OUTPUT=fancy python -m tests.dashboard_demo
"""

from __future__ import annotations

import os
import sys
import textwrap

from lograil import ProcessSpec, configure_logging, run_process_group


def _python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def _script(body: str) -> str:
    return textwrap.dedent(body).strip()


def _looping_tool(
    *,
    verb: str,
    items: list[str],
    delay: float,
    exit_code: int = 0,
) -> list[str]:
    return _python(
        _script(
            f"""
            import sys, time
            for item in {items!r}:
                print(f'{verb} {{item}}', file=sys.stderr, flush=True)
                time.sleep({delay!r})
            raise SystemExit({exit_code})
            """
        )
    )


def _pytest_tool(
    *,
    tests: list[str],
    collect_delay: float,
    test_delay: float,
) -> list[str]:
    total = len(tests)
    lines = [
        f"{nodeid} PASSED [{round(index / total * 100):>3}%]"
        for index, nodeid in enumerate(tests, start=1)
    ]
    return _python(
        _script(
            f"""
            import sys, time
            print('collecting ...', file=sys.stderr, flush=True)
            time.sleep({collect_delay!r})
            print('collected {total} items', file=sys.stderr, flush=True)
            for test in {lines!r}:
                time.sleep({test_delay!r})
                print(test, file=sys.stderr, flush=True)
            """
        )
    )


def main() -> int:
    os.environ.setdefault("LOGRAIL_OUTPUT", "fancy")
    configure_logging()
    specs = [
        ProcessSpec(
            _looping_tool(
                verb="checking",
                items=["package-a", "package-b", "package-c"],
                delay=1.05,
            ),
            name="mypy",
            category="typeck",
            subject="workspace",
        ),
        ProcessSpec(
            _looping_tool(
                verb="checking",
                items=["api", "sources", "dashboard"],
                delay=0.95,
            ),
            name="pyright",
            category="typeck",
        ),
        ProcessSpec(
            _looping_tool(
                verb="checking",
                items=["models", "tests", "plugins"],
                delay=1.1,
            ),
            name="basedpyright",
            category="typeck",
        ),
        ProcessSpec(
            _looping_tool(
                verb="checking",
                items=["imports", "generics", "protocols"],
                delay=0.85,
            ),
            name="pyrefly",
            category="typeck",
        ),
        ProcessSpec(
            _looping_tool(
                verb="linting",
                items=["src/lograil", "tests", "pyproject.toml"],
                delay=0.75,
            ),
            name="ruff",
            category="lint",
        ),
        ProcessSpec(
            _looping_tool(
                verb="format-checking",
                items=["src", "tests", "examples"],
                delay=0.65,
            ),
            name="black",
            category="lint",
        ),
        ProcessSpec(
            _looping_tool(
                verb="sorting",
                items=["runtime imports", "test imports", "demo imports"],
                delay=0.7,
            ),
            name="isort",
            category="lint",
        ),
        ProcessSpec(
            _looping_tool(
                verb="auditing",
                items=["yaml", "workflows", "permissions"],
                delay=0.8,
            ),
            name="actionlint",
            category="lint",
        ),
        ProcessSpec(
            _looping_tool(
                verb="scanning",
                items=["policies", "secrets", "shell"],
                delay=0.9,
            ),
            name="zizmor",
            category="lint",
        ),
        ProcessSpec(
            _pytest_tool(
                tests=[
                    "tests/test_api.py::test_exports",
                    "tests/test_tail.py::test_stream",
                    "tests/test_group.py::test_status",
                    "tests/test_group.py::test_failure",
                    "tests/test_demo.py::test_finish",
                ],
                collect_delay=0.9,
                test_delay=1.2,
            ),
            name="pytest",
            category="test",
            subject="unit",
            parser="pytest",
            kind="pytest",
        ),
        ProcessSpec(
            _pytest_tool(
                tests=[
                    "tests/integration/test_cli.py::test_plain_mode",
                    "tests/integration/test_cli.py::test_json_mode",
                    "tests/integration/test_cli.py::test_fancy_mode",
                    "tests/integration/test_cli.py::test_exit_codes",
                ],
                collect_delay=1.2,
                test_delay=1.4,
            ),
            name="pytest-integration",
            category="test",
            subject="integration",
            parser="pytest",
            kind="pytest",
        ),
        ProcessSpec(
            _pytest_tool(
                tests=[
                    "tests/e2e/test_dashboard.py::test_grouped_rows",
                    "tests/e2e/test_dashboard.py::test_progress_rows",
                    "tests/e2e/test_dashboard.py::test_failure_state",
                ],
                collect_delay=1.5,
                test_delay=1.6,
            ),
            name="pytest-e2e",
            category="test",
            subject="e2e",
            parser="pytest",
            kind="pytest",
        ),
        ProcessSpec(
            _pytest_tool(
                tests=[
                    "tests/smoke/test_import.py::test_public_api",
                    "tests/smoke/test_import.py::test_version",
                    "tests/smoke/test_import.py::test_console",
                ],
                collect_delay=0.7,
                test_delay=1.1,
            ),
            name="pytest-smoke",
            category="test",
            subject="smoke",
            parser="pytest",
            kind="pytest",
        ),
        ProcessSpec(
            _python(
                _script(
                    """
                    import sys, time
                    print('checking annotations', file=sys.stderr, flush=True)
                    time.sleep(3.6)
                    print(
                        'found type mismatch in demo',
                        file=sys.stderr,
                        flush=True,
                    )
                    raise SystemExit(1)
                    """
                )
            ),
            name="ty",
            category="typeck",
        ),
        ProcessSpec(
            _looping_tool(
                verb="checking",
                items=["package graph", "exports", "stubs"],
                delay=1.0,
            ),
            name="stubtest",
            category="typeck",
        ),
    ]
    result = run_process_group(specs)
    print()
    print("results")
    for process in result.processes:
        marker = "ok" if process.success else "failed"
        print(f"{process.spec.label}: {marker} exit={process.exit_code}")
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
