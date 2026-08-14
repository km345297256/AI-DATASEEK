FROM docker.1ms.run/python:3.12-slim

WORKDIR /app

# Keep image builds reliable on hosts where direct access to the Debian CDN is
# unavailable. This matches the mirrors already used for Python dependencies.
RUN sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources && \
    printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "60";' \
        'Acquire::https::Timeout "60";' \
        > /etc/apt/apt.conf.d/80-retries && \
    apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Install nodejs
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs

# Install uv from the same mirror used for project dependencies.
RUN pip install --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ uv

# Configure uv to use Aliyun mirror
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Install dependencies
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy project files
COPY backend/. .
COPY tools /opt/ai-dataseek/tools

# Set environment variables
ENV PYTHONPATH=/app
ENV TOOL_PLUGINS_DIR=/opt/ai-dataseek/tools

# Expose port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
