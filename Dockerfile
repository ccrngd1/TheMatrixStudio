# SPDX-License-Identifier: Apache-2.0
# Multi-stage Dockerfile for TheMatrix Simulation Studio

# Stage 1: Node build stage (placeholder for Phase 1 frontend)
FROM node:20-alpine AS frontend-builder
# Phase 0: No frontend yet, this stage is a no-op placeholder
# Phase 1 will build React/Vite frontend here
WORKDIR /app
RUN echo "Phase 0: No frontend build" > /app/placeholder.txt

# Stage 2: Python application
FROM python:3.11-slim AS app

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml ./
COPY matrix_studio/ matrix_studio/
COPY examples/ examples/

# Install Python package
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# Create data directory
RUN mkdir -p /app/data

# Expose port for future API (Phase 1)
EXPOSE 8000

# Default command: run a simulation
# Usage: docker run --env-file .env -v $(pwd)/examples:/examples <image> python -m matrix_studio /examples/minimal.json
CMD ["python", "-m", "matrix_studio", "--help"]
