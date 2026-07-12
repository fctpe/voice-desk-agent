# Container image for running the agent worker in server ("start") mode.
# The console demo is meant for a local machine with audio devices; this image
# targets a LiveKit deployment (see docs/telephony.md).
FROM python:3.12-slim

# uv for fast, reproducible installs from the lockfile.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

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

# Default to worker/server mode; override the command for other modes.
ENTRYPOINT ["uv", "run", "python", "-m", "voice_desk"]
CMD ["start"]
