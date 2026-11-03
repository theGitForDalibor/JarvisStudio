# Multi-stage production container build for Jarvis Studio
FROM python:3.12-slim AS base

# Prevent Python from writing .pyc files and enable unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# Install system dependencies (build tools, sqlite runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    build-essential \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install uv package manager
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency manifests first for layer caching
COPY pyproject.toml uv.lock ./

# Install runtime dependencies
RUN uv sync --frozen --no-dev --no-install-project

# Copy application source code
COPY config ./config
COPY src ./src
COPY README.md LICENSE ./

# Install the project wheel
RUN uv sync --frozen --no-dev

# Create data and logs mount directories
RUN mkdir -p /app/data /app/logs

EXPOSE 8000

# Container health probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/v1/system/health || exit 1

ENTRYPOINT ["uv", "run", "jarvis-studio", "serve", "--host", "0.0.0.0", "--port", "8000"]
