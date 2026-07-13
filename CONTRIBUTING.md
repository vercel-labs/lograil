# Contributing to lograil

Thanks for your interest in contributing. This guide covers how to get the repo
running locally and land a change.

## Signed commits

This repository requires verified commit signatures on protected branches.

Before contributing, configure Git to sign your commits with a GitHub-verified
GPG, SSH, or S/MIME key. Unsigned commits will be rejected by repository rules
and need to be rewritten as signed commits before they can be merged.

If a pull request includes unsigned commits, re-sign the commits and force-push
the branch. Make sure the signing key is added to your GitHub account and that
your commits appear as `Verified`.

A `Signed-off-by` line in the commit message is not enough to satisfy this
requirement. A verified commit signature alone does not satisfy the DCO either;
commits need both.

## Prerequisites

- **Python 3.10+** - the package supports Python 3.10 and newer.
- **uv** - dependency management, local environments, and builds use
  [uv](https://docs.astral.sh/uv/).

## Getting started

```bash
git clone https://github.com/vercel/lograil.git
cd lograil
uv sync --frozen --group=test --group=check
uv run pytest
```

The repo is a Python package with a `src/` layout:

- [`src/lograil`](./src/lograil) - public package and bundled log sources.
- [`src/lograil/_internal`](./src/lograil/_internal) - internal rendering,
  tailing, process, formatting, and CLI implementation.
- [`tests`](./tests) - unit, integration, and behavior tests.
- [`.github/workflows`](./.github/workflows) - CI and publish workflows.

## Development

Run the CLI through uv while iterating:

```bash
uv run lograil --help
```

For package changes, keep behavior covered by focused tests under `tests/`.
Public API changes should also update [`README.md`](./README.md) when user
behavior or documented examples change.

## Testing

```bash
uv run --frozen --no-default-groups --group=test pytest
```

Some tests exercise optional integrations such as Docker, file watching, and
VictoriaLogs parsing. Keep tests deterministic and avoid requiring external
services unless the test is already explicitly scoped to that integration.

## Linting, formatting, and type checking

```bash
uv run --frozen --no-default-groups --group=check ruff format --check src tests
uv run --frozen --no-default-groups --group=check ruff check src/lograil tests pyproject.toml
uv run --frozen --no-default-groups --group=check mypy
uv run --frozen --no-default-groups --group=check ty check
uv run --frozen --no-default-groups --group=check zizmor .github/workflows
```

These checks run in CI. Running them locally before pushing saves a review
round trip.

## Packaging

Build distributions with:

```bash
uv build
```

The package metadata lives in [`pyproject.toml`](./pyproject.toml). If a change
affects published package metadata, optional dependencies, entry points, or
included files, verify the generated wheel and source distribution before
opening a pull request.

## Documentation

User-facing docs live in [`README.md`](./README.md). If your change alters
public behavior, update the relevant documentation in the same pull request.

## Before opening a pull request

Every pull request must be tied to an issue. Before opening a PR, search the
existing issues, discussions, and pull requests so you do not duplicate active
work. If there is no existing issue, open one with the relevant template and
describe the problem, use case, or bug reproduction.

For changes to public APIs, log rendering behavior, process orchestration,
source integrations, dependencies, generated artifacts, packaging, or any
non-trivial implementation detail, wait for discussion on the issue before
investing in the implementation. The goal is to agree that the problem is real
and that the proposed direction fits lograil before review shifts to code. Bug
fixes should link to an issue with a reproduction or failing test case so the
problem remains tracked even if a specific fix is not accepted.

To avoid PRs that are unlikely to be reviewed or merged:

- Do not send broad rewrites, style-only churn, formatting-only changes, or
  generated-output refreshes unless a maintainer asked for them.
- Do not bundle unrelated fixes or refactors into one PR. Split them so each PR
  has one reviewable purpose.
- Do not add runtime dependencies without prior agreement. Prefer standard
  library behavior or narrowly scoped optional dependencies when possible.
- Do not change public behavior based only on a hypothetical use case. Include
  a concrete user story, reproduction, fixture, or test that shows the need.
- Do not claim an issue silently. Comment before starting work, and check the
  thread first in case someone else is already working on it.

## Submitting a pull request

1. Fork the repo and create a branch from `main`.
2. Link the issue where the change was discussed and agreed on.
3. Make your change, including tests and docs where relevant.
4. Sign off every commit with `git commit -s`.
5. Make sure the relevant tests, lint checks, and type checks pass.
6. Open the PR with a clear description of the problem and solution.

Releases are managed by the maintainers.

## Developer Certificate of Origin (DCO)

We do not require a CLA. Instead, all contributions are made under the
[Developer Certificate of Origin (DCO)](./DCO.txt), a lightweight, one-line
attestation that you have the right to submit your contribution under the
project's license. There is nothing to sign and no account to create.

Every commit must include a `Signed-off-by` line matching the commit author's
name and email:

```text
Signed-off-by: Jane Doe <jane.doe@example.com>
```

Add it automatically with:

```bash
git commit -s -m "your commit message"
```

If you forget, amend the last commit:

```bash
git commit --amend -s --no-edit
```

To sign off a series of commits, rebase with `--signoff`:

```bash
git rebase --signoff main
```

The sign-off requirement applies to all contributors, including Vercel
employees. A required check blocks pull requests that contain commits without a
valid sign-off.

## Reporting bugs and requesting features

Please use the issue templates at
<https://github.com/vercel/lograil/issues/new/choose>. For security issues, do
not open a public issue.

## Code of conduct

This project follows the [Code of Conduct](./CODE_OF_CONDUCT.md). By
participating, you agree to uphold it.

## License

`lograil` is licensed under the [Apache License 2.0](./LICENSE). By
contributing, you agree that your contributions will be licensed under that
same license (inbound = outbound).
