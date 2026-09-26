# Contributing to MCP Relay

MCP Relay is experimental, pre-1.0 software. Contributions should preserve
its deliberately narrow authority surface and keep claims aligned with runtime
evidence.

## Before you start

Read:

- [`README.md`](README.md) for the current product boundary;
- [`AGENTS.md`](AGENTS.md) for repository-wide engineering and security rules;
- [`docs/protocol.md`](docs/protocol.md) and [`docs/tools.md`](docs/tools.md) for
  the current public contracts;
- [`SECURITY.md`](SECURITY.md) before reporting a vulnerability.

For a non-trivial change, open or reference an issue that states the intended
behavior, non-goals and acceptance evidence. Implementation plans belong in
issues and pull requests rather than speculative repository roadmaps.

## Development setup

Install [`uv`](https://docs.astral.sh/uv/), clone the repository, then run:

```sh
uv sync --locked --group dev
```

Use synthetic fixtures and temporary workspaces. Never use personal
credentials, sessions, files, desktops, or external websites as test data.
The suite stays lightweight: no graphical environment, no browser, and no
specific MCP server package is required to run it.

## Local checks

Run the narrowest relevant tests first. Before requesting review, run:

```sh
uv run --frozen python -m pytest -q -m "not integration"
uv run --frozen python -m pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

Dependency changes must update `pyproject.toml` and regenerate `uv.lock` with
`uv`; do not hand-edit the lockfile. For a named dependency update, regenerate
only that package and inspect the resulting graph:

```sh
uv lock --upgrade-package <package>
uv lock --check
uv tree --locked
```

If a platform-specific check cannot run locally, state that clearly in the
pull request and identify the exact evidence still required.

## Change rules

- Keep the relayed MCP surface closed and typed.
- Do not add generic shell execution, arbitrary paths, or unrestricted
  provider passthrough to the public API.
- Preserve authentication, descriptor policy, bounded outputs, timeouts,
  cancellation, and process cleanup.
- Add or update tests when behavior or a public contract changes.
- Keep documentation claims narrower than the evidence. A preflight, mock, or
  unit test is not product end-to-end proof.
- Never commit secrets, tokens, personal data, or unsanitized artifacts to
  the repository; relay-owned output (diagnostics, error messages, logs)
  never exposes secret material either.

## Pull requests

Keep each pull request focused. Include:

- a concise description of the problem and solution;
- affected security and compatibility boundaries;
- exact commands run and their results;
- unrun or externally blocked checks;
- rollout or rollback notes when applicable.

By contributing, you agree that your contribution is licensed under the
repository's [MIT License](LICENSE).
