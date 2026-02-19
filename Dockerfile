# ATC Recorder Docker Image

FROM python:3.11-slim

# Labels for container metadata
LABEL org.opencontainers.image.title="ATC Recorder"
LABEL org.opencontainers.image.description="Record and download ATC audio from LiveATC.net"
LABEL org.opencontainers.image.version="0.1.0"

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    sox \
    libsox-fmt-mp3 \
    curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash atc

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt requirements-transcriber.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-transcriber.txt

# Copy application code
COPY --chown=atc:atc pyproject.toml README.md ./
COPY --chown=atc:atc src/ ./src/
COPY --chown=atc:atc config.yaml ./

# Install the package with dashboard visualization extras
RUN pip install --no-cache-dir ".[dashboard-viz]"

# Create directories with proper permissions
RUN mkdir -p /app/recordings /app/logs \
    && chown -R atc:atc /app

# Switch to non-root user
USER atc

# Environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check - verify the CLI is working (quiet mode for cleaner logs)
HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
    CMD atc-recorder check --quiet || exit 1

# Default command
ENTRYPOINT ["atc-recorder"]
CMD ["--help"]
