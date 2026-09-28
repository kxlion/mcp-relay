# MCP Relay contributor contract

This file defines the repository-wide working agreement for every coding agent.

Direct user instructions override workflow preferences in this file. They do
not silently waive security boundaries or evidence requirements. External
actions require an explicit user request or approval. If an instruction
conflicts with a critical boundary, stop and surface the conflict.

## Project mission

MCP Relay is a generic, bidirectional MCP relay, neutral with respect to the
MCP servers it relays. The project does not install, test, or guarantee any
specific MCP server (no CUA, no Blender, no bundled server). It guarantees
only MCP protocol relaying, transport, reconnection, and lifecycle handling.

The public request path is:

```text
MCP client -> /mcp -> Relay Server -> authenticated WebSocket
           -> Relay Client -> local MCP servers -> closed result
```

Local MCP servers are aggregated behind a single remote MCP facade under
user-configured aliases. Their tools are published as native MCP tools named
`<alias>__<tool>`, optionally restricted by the entry's `tools:` allowlist.
The relay forwards messages without functional interpretation and preserves
request identifiers and correlations.

## Sources of truth

Read only the files relevant to the task. Use `README.md` and `CONTRIBUTING.md`
for project scope and workflow, then inspect the relevant code, tests, and
documentation for the behavior being changed.

When these sources disagree, surface the drift. When making a change, update the
smallest coherent set of code, tests, and documentation.

## Setup commands

MCP Relay requires Python 3.14 or newer and uses `uv` for dependency and
environment management.

Install the locked development environment with:

```sh
uv sync --locked --group dev
```

Do not hand-edit `uv.lock`. Regenerate it with `uv` only when dependency changes
are part of the task.

## Testing instructions

Run the narrowest relevant test first. Before reporting completion, run the
applicable repository checks:

```sh
uv run --frozen python -m pytest -q -m "not integration"
uv run --frozen python -m pytest -q -m integration
uv run --frozen ruff check .
uv lock --check
git diff --check
```

- Add or update focused tests when behavior or a public contract changes.
- For a defect, add a regression test that reproduces the failure when practical.
- Report only checks and runtime evidence that were actually obtained. A mock,
  preflight, or unit test is not product end-to-end proof.
- Tests must stay lightweight and CUA-independent: no graphical environment,
  no browser, no CUA package or driver is required to run the suite.

## Code and change guidelines

- Inspect the current Git status before editing and preserve pre-existing or
  unrelated worktree changes.
- Keep changes focused. Avoid unrelated refactors, dependency upgrades,
  formatting churn, or speculative functionality.
- Legacy has no place in this repository. When a public surface (CLI command,
  flag, config key, documented behavior) is deliberately removed, the removal
  is final: do not reintroduce it, do not keep shims, aliases, or
  compatibility layers for it, and do not add tests that assert its past
  existence. Existing users migrate via the release notes, not via retained
  dead code.
- Follow the Python and Ruff settings in `pyproject.toml`.
- Update documentation in the same change when behavior, setup, limits, or
  public claims change.
- Distinguish local test results from CI or live runtime evidence.

## Security considerations

- Keep relayed capabilities explicit and bounded: the facade publishes
  `relay_status`, `relay_registry_search`, the five administration tools only
  when the Client has `admin: true`, and the Client's bounded catalog. A call
  reaches a third-party tool only if it is in the published catalog.
- Server and Client share one relay contract; a wire change bumps
  `RELAY_CONTRACT` and both sides are upgraded together.
- Preserve authentication, authorization boundaries, input validation, bounded
  outputs, timeouts, cancellation, and cleanup when changing those areas.
- Tokens (Relay Client Token, public MCP access token) never appear in the
  YAML configuration nor in `config show` output.
- Relay-owned output never exposes API keys.
- Relayed third-party payloads pass through without secret scanning — the
  relay owns transport and bounds, not payload policy.
- Never commit credentials, tokens, personal data, or unsanitized artifacts
  to the repository.
- Use synthetic fixtures, temporary workspaces, and isolated profiles or
  sessions for tests.

## Git and external actions

Do not create commits or tags, push changes, open or modify pull requests,
trigger remote workflows, publish artifacts, deploy infrastructure, or connect
external accounts without explicit user approval.
