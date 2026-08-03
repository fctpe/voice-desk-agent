# Container image for running the agent worker in server ("start") mode.
# The console demo is meant for a local machine with audio devices; this image
# targets a LiveKit deployment (see docs/telephony.md).
FROM python:3.12-slim

# uv for fast, reproducible installs from the lockfile. Pinned: `uv:latest`
# makes the image non-reproducible across rebuilds.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (better layer caching), then the project.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev

# Evals are handy to run inside the image too.
COPY evals ./evals

# Pre-download plugin model files (e.g. Silero VAD) so the first call is fast.
RUN uv run python -m livekit.agents download-files || true

# Run unprivileged. Model files downloaded above stay root-owned and readable;
# the agent only reads them.
RUN useradd --system --create-home --uid 1001 app
USER app
ENV HOME=/home/app

# Default to worker/server mode; override the command for other modes.
ENTRYPOINT ["uv", "run", "python", "-m", "voice_desk"]
CMD ["start"]
