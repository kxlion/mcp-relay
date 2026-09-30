# Security Policy

## Project status

MCP Relay is experimental, pre-1.0 software. It does not currently have a
production-ready or long-term-supported release. Security fixes are applied on
a best-effort basis to the current development line.

Read [`docs/deployment.md`](docs/deployment.md) and [`docs/cli.md`](docs/cli.md)
for the deployment boundaries (TLS proxy, private listeners, tokens) and
[`docs/protocol.md`](docs/protocol.md) for the relay contract and its bounds.
Do not expose the Relay Server directly to the public internet or use personal sessions, profiles, credentials or data
for testing.

## Reporting a vulnerability

Do not publish vulnerability details in a public issue, discussion, pull
request, CI log or artifact.

1. If the repository's **Security** tab provides **Report a vulnerability**, use
   that private reporting form.
2. If private reporting is unavailable, open a minimal public issue asking the
   maintainer to establish a private channel. Do not include exploit details,
   tokens, personal data or sensitive logs in that issue.

A useful report includes:

- the affected commit or version;
- the affected component and deployment topology;
- reproducible steps using synthetic data;
- expected and observed behavior;
- practical impact and any known workaround;
- sanitized logs or artifacts only when necessary.

Relevant issues include authentication or authorization bypass, credential
exposure, arbitrary command or path access, unsafe parsing, process-cleanup
failures, resource exhaustion, sensitive artifact leakage and CI supply-chain
risks.

## Response

The maintainers will acknowledge and triage reports on a best-effort basis.
There is no guaranteed response or remediation SLA while the project remains
pre-1.0. Please allow time to reproduce and fix a report before public
disclosure.
