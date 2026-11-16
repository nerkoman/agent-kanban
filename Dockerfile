# Build stage
FROM python:3.12-slim AS builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy project files
COPY pyproject.toml requirements.txt uv.lock ./

# Install dependencies
RUN uv sync --frozen --no-install-project

# Runtime stage
FROM python:3.12-slim

WORKDIR /app

# Install uv for runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Create non-root user
RUN useradd -m -u 1000 appuser

# Copy application code
COPY kanban_ui/ kanban_ui/
COPY kanban_mcp/ kanban_mcp/
COPY kanban_store/ kanban_store/
COPY seed/ seed/
COPY scripts/ scripts/

# Copy documentation and examples
COPY README.md QUICKSTART.md CHANGELOG.md CONTRIBUTING.md LICENSE ./
COPY docs/ docs/
COPY examples/ examples/

# Copy project files for uv sync
COPY pyproject.toml requirements.txt uv.lock ./

# Create data directories with proper permissions
RUN mkdir -p /app/kanban_data/inbox /app/kanban_data/logs /app/kanban_data/rules /app/kanban_data/webhooks && \
    chown -R appuser:appuser /app/kanban_data

# Install dependencies in runtime image
RUN uv sync --frozen --no-dev

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 7777

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV KANBAN_HOST=0.0.0.0
ENV KANBAN_PORT=7777
ENV KANBAN_DB=/app/kanban_data/tasks.db
ENV KANBAN_INBOX_DIR=/app/kanban_data/inbox
ENV KANBAN_RULES_FILE=/app/kanban_data/rules.json
ENV KANBAN_WEBHOOKS_FILE=/app/kanban_data/webhooks.json

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7777/health', timeout=5)" || exit 1

# Run the application
CMD ["uv", "run", "python", "-m", "kanban_ui"]
