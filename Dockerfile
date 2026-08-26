# One image for every service; compose picks the command.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.28 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app
# "--extra pg" by default. Add "--extra local" for MiniLM embeddings (pulls in torch, and needs
# .models/ mounted; see README).
ARG EXTRAS="--extra pg"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project $EXTRAS

COPY summerand/ summerand/
COPY config/ config/
COPY fixtures/ fixtures/
COPY models.lock ./
RUN uv sync --frozen --no-dev $EXTRAS

RUN useradd --create-home app && mkdir -p /app/var && chown app /app/var
USER app
EXPOSE 8000
CMD ["summerand", "api", "--host", "0.0.0.0"]
