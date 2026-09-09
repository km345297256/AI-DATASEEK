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
    apt-get install -y --no-install-recommends curl ca-certificates p7zip-full && \
    rm -rf /var/lib/apt/lists/*

# Install nodejs
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y nodejs && \
    node -e "const [major, minor] = process.versions.node.split('.').map(Number); if (major !== 22 || minor < 19) process.exit(1)"

# Build the Cordis catalog host once and keep only its production runtime in
# the backend image. It is supervised by FastAPI over stdio and exposes no
# additional network port.
ARG NPM_REGISTRY=https://registry.npmmirror.com
COPY plugin-host/package.json plugin-host/package-lock.json /opt/ai-dataseek/plugin-host/
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --prefix /opt/ai-dataseek/plugin-host \
        --prefer-offline --no-audit --registry="${NPM_REGISTRY}"
COPY plugin-host/tsconfig.json /opt/ai-dataseek/plugin-host/tsconfig.json
COPY plugin-host/src /opt/ai-dataseek/plugin-host/src
COPY plugin-host/visualizations /opt/ai-dataseek/plugin-host/visualizations
RUN npm run build --prefix /opt/ai-dataseek/plugin-host && \
    npm prune --prefix /opt/ai-dataseek/plugin-host --omit=dev --no-audit

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
COPY sandbox/app /opt/ai-dataseek/sandbox-contract/app
COPY sandbox/scripts /opt/ai-dataseek/sandbox-contract/scripts
COPY sandbox/scientific_operators /opt/ai-dataseek/sandbox-contract/scientific_operators
COPY sandbox/Dockerfile /opt/ai-dataseek/sandbox-contract/Dockerfile
COPY sandbox/pyproject.toml /opt/ai-dataseek/sandbox-contract/pyproject.toml
COPY sandbox/supervisord.conf /opt/ai-dataseek/sandbox-contract/supervisord.conf
COPY sandbox/uv.lock /opt/ai-dataseek/sandbox-contract/uv.lock

# Set environment variables
ENV PYTHONPATH=/app
ENV TOOL_PLUGINS_DIR=/opt/ai-dataseek/tools
ENV PLUGIN_RUNTIME_HOST_PATH=/opt/ai-dataseek/plugin-host/dist/index.js
ENV PLUGIN_RUNTIME_TOOLS_DIR=/opt/ai-dataseek/tools
ENV PLUGIN_RUNTIME_EXECUTION_CONTRACT_DIR=/opt/ai-dataseek/sandbox-contract

# Expose port
EXPOSE 8000

# Start command
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
