FROM python:3.14.4-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /uvx /bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.14.4-slim-bookworm AS runtime

RUN apt-get update \
    && apt-get install --no-install-recommends -y ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --home-dir /home/relay --shell /usr/sbin/nologin relay

COPY --from=builder /opt/venv /opt/venv

ENV HOME=/home/relay \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace
USER relay

ENTRYPOINT ["mcp-relay"]
