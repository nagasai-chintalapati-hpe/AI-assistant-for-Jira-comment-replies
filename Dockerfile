# ── Build stage ──────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# System deps needed for llama-cpp-python, chromadb, sentence-transformers
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

# Install production deps only (no dev/test tools)
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -e ".[prod]"

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime system libs only
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy installed packages + source from builder
COPY --from=builder /usr/local/lib/python3.11 /usr/local/lib/python3.11
COPY --from=builder /usr/local/bin /usr/local/bin
COPY --from=builder /app /app

# Persistent data volume mount point
RUN mkdir -p /app/.data

# Non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Gunicorn + uvicorn workers for production concurrency
CMD ["uvicorn", "src.api.app:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "2", \
     "--log-level", "info"]
