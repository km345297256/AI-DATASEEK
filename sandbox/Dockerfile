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
    zip \
    unzip \
    unrar \
    p7zip-full \
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
    netcdf-bin \
    hdf5-tools \
    proj-bin \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Archive inspection is a common dataset operation. Keep it deterministic: the
# agent must never need to download or compile extractors at task runtime.
RUN command -v zip && \
    command -v unzip && \
    command -v unrar && \
    command -v 7z && \
    unzip -v | grep -F 'UnZip' && \
    unrar | grep -F 'UNRAR' && \
    7z i | grep -F '7-Zip'

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

# Keep application services and user analysis commands on the same uv-managed
# interpreter. Including pip in the locked project dependencies ensures that
# `python`, `python3`, `pip`, and `pip3` all resolve inside this environment.
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/app/.venv \
    PATH=/app/.venv/bin:${PATH}

# Keep the distro GDAL binding available while preferring the locked NumPy and
# visualization stack from the virtual environment.
ENV PYTHONPATH=/usr/local/lib/ai-dataseek:/app/.venv/lib/python3.10/site-packages:/usr/lib/python3/dist-packages

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
    MATPLOTLIBRC=/etc/matplotlibrc \
    MPLBACKEND=Agg
#
## Make matplotlib render Chinese text by default instead of falling back to DejaVu Sans.
RUN printf '%s\n' \
    'font.family: sans-serif' \
    'font.sans-serif: Noto Sans CJK SC, Noto Sans CJK JP, Noto Sans CJK TC, Noto Sans CJK KR, DejaVu Sans' \
    'axes.unicode_minus: False' \
    > /etc/matplotlibrc && \
    chmod 0644 /etc/matplotlibrc

# Set working directory
WORKDIR /app

# Copy dependency files first to leverage the Docker layer cache.
COPY pyproject.toml uv.lock ./
COPY scripts/install_noto_cjk_sc.py scripts/verify_matplotlib_cjk.py scripts/verify_geoscience_stack.py scripts/recursive_unpack.py scripts/dataset_quicklook.py scripts/dataset_analysis_runner.py scripts/scientific_data_ops.py /usr/local/lib/ai-dataseek/

# Install the exact locked runtime, including the common Excel and plotting
# stack. Avoid retaining uv's download cache in the final image layer.
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Give agents one deterministic command for nested dataset archives instead of
# spending model turns discovering and chaining format-specific extractors.
RUN install -m 0755 \
    /usr/local/lib/ai-dataseek/recursive_unpack.py \
    /usr/local/bin/ai-dataseek-unpack && \
    install -m 0755 \
    /usr/local/lib/ai-dataseek/dataset_quicklook.py \
    /usr/local/bin/ai-dataseek-quicklook && \
    install -m 0755 \
    /usr/local/lib/ai-dataseek/dataset_analysis_runner.py \
    /usr/local/bin/ai-dataseek-analysis && \
    install -m 0755 \
    /usr/local/lib/ai-dataseek/scientific_data_ops.py \
    /usr/local/bin/ai-dataseek-scientific

# Debian/Ubuntu ships Noto CJK as a TTC. Matplotlib only indexes the first TTC
# face, which is JP, even though fontconfig can resolve the SC face. Extract
# standalone SC Regular/Bold OTF files with Matplotlib's locked fontTools
# dependency so findfont can select the configured family without fallback.
RUN python /usr/local/lib/ai-dataseek/install_noto_cjk_sc.py && \
    fc-cache -f && \
    fc-match -f '%{family}\n' 'Noto Sans CJK SC' | grep -F 'Noto Sans CJK SC'

# Fail the image build if task commands would see a different Python/pip pair
# or if the preinstalled scientific stack is incomplete.
RUN python -m pip --version && \
    python -c "import matplotlib, numpy, openpyxl, pandas, rarfile, rasterio, seaborn, xlrd; from PIL import Image; from osgeo import gdal; assert tuple(map(int, rasterio.__version__.split('.')[:2])) >= (1, 4); assert int(numpy.__version__.split('.')[0]) < 2; assert gdal.VersionInfo(); print('scientific stack ready', 'rasterio=' + rasterio.__version__, 'rasterio_gdal=' + rasterio.__gdal_version__, 'system_gdal=' + gdal.VersionInfo())" && \
    command -v ncdump && \
    command -v h5dump && \
    command -v projinfo && \
    projinfo EPSG:4326 >/dev/null && \
    python /usr/local/lib/ai-dataseek/verify_geoscience_stack.py && \
    ai-dataseek-unpack --help >/dev/null && \
    ai-dataseek-quicklook --help >/dev/null && \
    ai-dataseek-analysis --help >/dev/null && \
    ai-dataseek-scientific --help >/dev/null && \
    python /usr/local/lib/ai-dataseek/verify_matplotlib_cjk.py && \
    install -d -o ubuntu -g ubuntu /home/ubuntu/.cache/matplotlib && \
    sudo -u ubuntu -H env \
        MATPLOTLIBRC=/etc/matplotlibrc \
        MPLBACKEND=Agg \
        /app/.venv/bin/python /usr/local/lib/ai-dataseek/verify_matplotlib_cjk.py

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
