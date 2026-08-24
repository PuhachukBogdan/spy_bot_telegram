# --- stage 1: build the report shell -----------------------------------------
# Node is a BUILD-only dependency. Only the single generated .html crosses into
# the runtime image, so the shipped container stays a plain Python image with no
# node_modules and no JS toolchain.
FROM node:22-slim AS shell

WORKDIR /build
# Lockfile first for layer caching: dependencies only reinstall when they change.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


# --- stage 2: runtime ---------------------------------------------------------
FROM python:3.11-slim

# System dependencies: ffmpeg for video_note audio extraction
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first (better layer caching)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir .

# Copy source
COPY src/ ./src/
COPY prompts/ ./prompts/

# The built report shell (one self-contained HTML). src/metrics/shell.py looks
# here first, so the image never falls back to a stale local dev build.
COPY --from=shell /build/dist/index.html ./static/report-shell.html

# Non-root user
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

# Healthcheck hits the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8080/health || exit 1

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
