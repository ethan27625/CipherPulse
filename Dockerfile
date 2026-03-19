# CipherPulse — automated Shorts pipeline
# Multi-stage build: builder installs deps, final image is lean.
#
# Build:  docker build -t cipherpulse .
# Run:    docker run --env-file .env -v $(pwd)/output:/app/output \
#                    -v $(pwd)/config:/app/config cipherpulse
# Dry run: docker run --env-file .env cipherpulse --dry-run

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build tools
RUN pip install --upgrade pip

# Copy and install Python dependencies into a prefix we can copy over
COPY requirements.txt .
RUN pip install --prefix=/install --no-cache-dir -r requirements.txt


# ── Final image ────────────────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL org.opencontainers.image.title="CipherPulse"
LABEL org.opencontainers.image.description="Automated cybersecurity Shorts pipeline"

WORKDIR /app

# System dependencies: FFmpeg (video assembly) + libass (subtitle rendering)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libass9 \
    fonts-liberation \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Copy source code
COPY src/       ./src/
COPY topics.json ./
COPY config/platforms.json ./config/platforms.json

# Runtime directories — mount these as volumes in production so
# outputs and OAuth tokens persist between runs
RUN mkdir -p output config assets/footage_cache assets/music

# Non-root user for security
RUN useradd -m -u 1001 cipherpulse && chown -R cipherpulse:cipherpulse /app
USER cipherpulse

# Entrypoint: run the orchestrator; any CLI flags passed via CMD
ENTRYPOINT ["python3", "-m", "src.orchestrator"]
CMD []
