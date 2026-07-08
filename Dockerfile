# SPDX-License-Identifier: Apache-2.0
# Multi-stage Dockerfile for TheMatrix Simulation Studio.
# One image, one port: the Python app serves both the REST/WS API and the
# built React frontend as static assets.

# ------------------------------------------------------------------------- #
# Stage 1: build the React/Vite/TS frontend into matrix_studio/static
# ------------------------------------------------------------------------- #
FROM node:20-alpine AS frontend-builder
WORKDIR /build

# Install deps first (better layer caching).
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && npm install

# Copy sources and build. Vite's outDir is ../matrix_studio/static (see
# vite.config.ts), so the build populates the Python package's static dir at
# /build/matrix_studio/static.
COPY frontend/ ./frontend/
RUN mkdir -p /build/matrix_studio && cd frontend && npm run build

# ------------------------------------------------------------------------- #
# Stage 2: Python application
# ------------------------------------------------------------------------- #
FROM python:3.11-slim AS app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project metadata + source
COPY pyproject.toml ./
COPY README.md ./
COPY matrix_studio/ matrix_studio/
COPY examples/ examples/

# Bring in the built frontend from the Node stage (served as static assets)
COPY --from=frontend-builder /build/matrix_studio/static/ matrix_studio/static/

# Install Python package
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Create data directory for the SQLite store
RUN mkdir -p /app/data
ENV DATA_DIR=/app/data
# Bind to all interfaces inside the container so the mapped port is reachable
ENV MATRIX_HOST=0.0.0.0
ENV MATRIX_PORT=8000

# Expose the control-room server port
EXPOSE 8000

# Default command: serve the control-room UI + API.
#   docker run -p 8000:8000 --env-file .env <image>
# The Phase 0 file-in/out CLI is still available:
#   docker run --env-file .env -v $(pwd)/examples:/examples <image> \
#       python -m matrix_studio run /examples/minimal.json
CMD ["python", "-m", "matrix_studio", "serve"]
