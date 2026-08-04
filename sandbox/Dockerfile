FROM docker.1ms.run/ubuntu:22.04

# Avoid interactive prompts during installation
ENV DEBIAN_FRONTEND=noninteractive

# Set hostname to sandbox
ENV HOSTNAME=sandbox

# Configure apt sources. Keep ubuntu-ports official by default because third-party
# ports mirrors are more likely to have incomplete arm64 package indexes.
ARG UBUNTU_MIRROR=http://mirrors.aliyun.com/ubuntu
ARG UBUNTU_PORTS_MIRROR=http://ports.ubuntu.com/ubuntu-ports
RUN sed -i "s|http://archive.ubuntu.com/ubuntu/|${UBUNTU_MIRROR}/|g" /etc/apt/sources.list && \
    sed -i "s|http://security.ubuntu.com/ubuntu/|${UBUNTU_MIRROR}/|g" /etc/apt/sources.list && \
    sed -i "s|http://ports.ubuntu.com/ubuntu-ports/|${UBUNTU_PORTS_MIRROR}/|g" /etc/apt/sources.list && \
    printf '%s\n' \
        'Acquire::Retries "5";' \
        'Acquire::http::Timeout "60";' \
        'Acquire::https::Timeout "60";' \
        > /etc/apt/apt.conf.d/80-retries

# Update and install basic tools
RUN apt-get update && apt-get install -y \
    sudo \
    bc \
    curl \
    wget \
    gnupg \
    software-properties-common \
    xvfb \
    x11vnc \
    xterm \
    socat \
    supervisor \
    websockify \
    python3-numpy \
    python3-gdal \
    gdal-bin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Create user ubuntu and grant sudo privileges
RUN useradd -m -d /home/ubuntu -s /bin/bash ubuntu && \
    echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu

# Install Python 3.10.12
RUN add-apt-repository ppa:deadsnakes/ppa && \
    apt-get update && \
    apt-get install -y python3.10 python3.10-venv python3.10-dev python3-pip && \
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1 && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install uv from the same reachable mirror used for project dependencies.
RUN pip3 install --no-cache-dir \
    --index-url https://mirrors.aliyun.com/pypi/simple/ uv

# Configure uv to use Aliyun mirror
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# Scientific task scripts run from the uv-managed venv. Keep the distro's
# binary-compatible NumPy/GDAL packages visible there without per-task installs.
ENV PYTHONPATH=/usr/lib/python3/dist-packages

# Install Node.js 20.18.0
RUN mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y nodejs && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Configure npm to use Aliyun mirror
RUN npm config set registry https://registry.npmmirror.com

# Install Google Chrome
RUN add-apt-repository ppa:xtradeb/apps -y && \
    apt-get update && \
    apt-get install -y chromium  --no-install-recommends && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

## Install Chinese fonts and language support
RUN apt-get update && apt-get install -y \
    fontconfig \
    fonts-noto-cjk \
    fonts-noto-color-emoji \
    language-pack-zh-hans \
    locales \
    && locale-gen zh_CN.UTF-8 \
    && fc-cache -fv \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
#
## Set default locale
ENV LANG=zh_CN.UTF-8 \
    LANGUAGE=zh_CN:zh \
    LC_ALL=zh_CN.UTF-8 \
    PYTHONUTF8=1 \
    PYTHONIOENCODING=UTF-8 \
    MATPLOTLIBRC=/etc/matplotlibrc
#
## Make matplotlib render Chinese text by default instead of falling back to DejaVu Sans.
RUN printf '%s\n' \
    'font.family: sans-serif' \
    'font.sans-serif: Noto Sans CJK SC, Noto Sans CJK JP, Noto Sans CJK TC, Noto Sans CJK KR, DejaVu Sans' \
    'axes.unicode_minus: False' \
    > /etc/matplotlibrc

# Set working directory
WORKDIR /app

# Copy project files (copy dependency files first to leverage cache)
COPY pyproject.toml ./

# Install Python dependencies
RUN uv sync --no-dev

# Copy remaining project files
COPY . .

# Ensure application files are writable by app user
RUN chown -R ubuntu:ubuntu /app

# Configure supervisor
COPY supervisord.conf /etc/supervisor/conf.d/app.conf

# Expose ports
EXPOSE 8080 9222 5900 5901

ENV UVI_ARGS=""
ENV CHROME_ARGS=""

# Use supervisor to start all services
CMD ["supervisord", "-n", "-c", "/app/supervisord.conf"] 
