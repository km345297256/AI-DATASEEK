FROM docker.1ms.run/ubuntu:22.04 AS database-reader-build

# A narrow libmdb adapter, not the MDB Tools SQL engine, GUI or exporters.
# Keep build dependencies out of the existing analysis runtime. The public
# source archive is pinned; the library remains dynamically replaceable.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config curl ca-certificates libglib2.0-dev && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/mdbtools-build
RUN curl --fail --location --retry 3 \
    https://github.com/mdbtools/mdbtools/releases/download/v1.0.1/mdbtools-1.0.1.tar.gz \
    -o /tmp/mdbtools-1.0.1.tar.gz && \
    echo 'ff9c425a88bc20bf9318a332eec50b17e77896eef65a0e69415ccb4e396d1812  /tmp/mdbtools-1.0.1.tar.gz' | sha256sum -c - && \
    tar -xzf /tmp/mdbtools-1.0.1.tar.gz --strip-components=1 && \
    ./configure --prefix=/opt/dataseek-database --disable-static --enable-shared --disable-man && \
    make -C src/libmdb -j2 && make -C src/libmdb install
COPY sandbox/app/services/access_table_native.c /opt/mdbtools-build/dataseek-access-reader.c
RUN cc -O2 -Wall -Wextra -Iinclude dataseek-access-reader.c \
    $(pkg-config --cflags --libs glib-2.0) -L/opt/dataseek-database/lib \
    -Wl,-rpath,/opt/dataseek-database/lib -lmdb -lm -o /opt/dataseek-access-reader

FROM docker.1ms.run/ubuntu:22.04 AS physical-reader-build
# Extract only the two offline utilities; no database daemon/client is shipped.
# Ubuntu archive signatures authenticate packages; exact versions fail closed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    mysql-server-core-8.0=8.0.46-0ubuntu0.22.04.4 rocksdb-tools=6.11.4-3 && \
    mkdir -p /opt/dataseek-physical/bin /opt/dataseek-physical/licenses && \
    cp /usr/bin/ibd2sdi /usr/bin/sst_dump /opt/dataseek-physical/bin/ && \
    cp /usr/share/doc/mysql-server-core-8.0/copyright /opt/dataseek-physical/licenses/mysql-copyright && \
    cp /usr/share/doc/rocksdb-tools/copyright /opt/dataseek-physical/licenses/rocksdb-copyright && \
    ibd2sdi --version | grep -F '8.0.46'

FROM docker.1ms.run/ubuntu:22.04

ARG TARGETARCH

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
    libreoffice-writer \
    libreoffice-impress \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-chi-sim \
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
COPY sandbox/pyproject.toml sandbox/uv.lock ./
COPY sandbox/scripts/install_noto_cjk_sc.py sandbox/scripts/verify_matplotlib_cjk.py sandbox/scripts/verify_geoscience_stack.py sandbox/scripts/recursive_unpack.py sandbox/scripts/dataset_quicklook.py sandbox/scripts/dataset_analysis_runner.py sandbox/scripts/scientific_data_ops.py sandbox/scripts/scientific_recipe_ops.py sandbox/scripts/tool_plugin_runner.py /usr/local/lib/ai-dataseek/
COPY sandbox/scientific_operators /usr/local/lib/ai-dataseek/scientific_operators

# Install the exact locked runtime, including the common Excel and plotting
# stack. numcodecs 0.13.1 does not publish a Linux arm64 wheel, and uv can
# misread the test fixture metadata in its source-built wheel. Let pip handle
# that one locked package on arm64, then keep uv in charge of everything else.
RUN if [ "$TARGETARCH" = "arm64" ]; then \
        UV_INDEX_URL=https://pypi.org/simple/ \
        uv sync --frozen --no-dev --no-install-project --no-install-package numcodecs --no-cache && \
        python -m pip install --no-cache-dir --no-deps numcodecs==0.13.1; \
    else \
        UV_INDEX_URL=https://pypi.org/simple/ \
        uv sync --frozen --no-dev --no-install-project --no-cache; \
    fi

# Verify the sandbox-only SQLite wrapper exposes the public resource controls.
RUN /app/.venv/bin/python -c "import apsw; assert apsw.apswversion() == '3.53.4.0'; c=apsw.Connection(':memory:'); c.limit(apsw.SQLITE_LIMIT_ATTACHED,0); assert c.limit(apsw.SQLITE_LIMIT_ATTACHED)==0; assert hasattr(c,'set_authorizer') and hasattr(c,'set_progress_handler'); c.close()"

COPY --from=database-reader-build /opt/dataseek-access-reader /usr/local/bin/dataseek-access-reader
COPY --from=database-reader-build /opt/dataseek-database/lib/libmdb.so* /opt/dataseek-database/lib/
COPY --from=database-reader-build /opt/mdbtools-build/COPYING.LIB /opt/dataseek-database/licenses/COPYING.LIB
RUN /app/.venv/bin/python -c "import duckdb,dbfread; assert duckdb.__version__=='1.5.5'; assert dbfread.__version__=='2.0.7'" && \
    test -x /usr/local/bin/dataseek-access-reader && \
    ldd /usr/local/bin/dataseek-access-reader && \
    ! ldd /usr/local/bin/dataseek-access-reader | grep -q 'not found'

# Optional visualization executables must not invalidate the base browser/desktop layers.
RUN apt-get update && apt-get install -y --no-install-recommends fastqc default-jre-headless && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Tool plugins change frequently and must not invalidate the scientific dependency layer.
COPY sandbox/scripts/geoscience_ops.py /usr/local/lib/ai-dataseek/
COPY tools /opt/ai-dataseek/tools

# Tool-specific dependencies stay with their plugins so adding a plugin does
# not invalidate and reinstall the full scientific runtime. FASTA/FASTQ and
# BLAST tools require Biopython at image build time because sandboxes cannot
# install packages at task runtime.
RUN test -s /opt/ai-dataseek/tools/sequence/requirements.txt && \
    grep -Eq '^biopython([<=>]|$)' /opt/ai-dataseek/tools/sequence/requirements.txt && \
    uv pip install --no-cache \
    --index-url https://mirrors.aliyun.com/pypi/simple/ \
    -r /opt/ai-dataseek/tools/documents/requirements.txt \
    -r /opt/ai-dataseek/tools/presentations/requirements.txt \
    -r /opt/ai-dataseek/tools/tabular/requirements.txt \
    -r /opt/ai-dataseek/tools/chemistry/requirements.txt \
    -r /opt/ai-dataseek/tools/xrd/requirements.txt \
    -r /opt/ai-dataseek/tools/jip/requirements.txt \
    -r /opt/ai-dataseek/tools/molecular/requirements.txt \
    -r /opt/ai-dataseek/tools/pxp/requirements.txt \
    -r /opt/ai-dataseek/tools/sequence/requirements.txt \
    -r /opt/ai-dataseek/tools/space/requirements.txt

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
    /usr/local/bin/ai-dataseek-scientific && \
    install -m 0755 \
        /usr/local/lib/ai-dataseek/scientific_recipe_ops.py \
        /usr/local/bin/ai-dataseek-scientific-recipe && \
    install -m 0755 \
        /usr/local/lib/ai-dataseek/tool_plugin_runner.py \
        /usr/local/bin/ai-dataseek-tool

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
    python -c "import Bio, matplotlib, numpy, openpyxl, pandas, rarfile, rasterio, seaborn, xlrd; from PIL import Image; from osgeo import gdal; assert tuple(map(int, rasterio.__version__.split('.')[:2])) >= (1, 4); assert int(numpy.__version__.split('.')[0]) < 2; assert gdal.VersionInfo(); print('scientific stack ready', 'biopython=' + Bio.__version__, 'rasterio=' + rasterio.__version__, 'rasterio_gdal=' + rasterio.__gdal_version__, 'system_gdal=' + gdal.VersionInfo())" && \
    command -v ncdump && \
    command -v h5dump && \
    command -v projinfo && \
    command -v tesseract && \
    tesseract --list-langs 2>&1 | grep -F 'chi_sim' && \
    projinfo EPSG:4326 >/dev/null && \
    python /usr/local/lib/ai-dataseek/verify_geoscience_stack.py && \
    ai-dataseek-unpack --help >/dev/null && \
    ai-dataseek-quicklook --help >/dev/null && \
    ai-dataseek-analysis --help >/dev/null && \
    ai-dataseek-scientific --help >/dev/null && \
    ai-dataseek-scientific-recipe --help >/dev/null && \
    ai-dataseek-tool list --json | grep -F 'scientific_inspect' && \
    python /usr/local/lib/ai-dataseek/verify_matplotlib_cjk.py && \
    install -d -o ubuntu -g ubuntu /home/ubuntu/.cache/matplotlib && \
    sudo -u ubuntu -H env \
        MATPLOTLIBRC=/etc/matplotlibrc \
        MPLBACKEND=Agg \
        /app/.venv/bin/python /usr/local/lib/ai-dataseek/verify_matplotlib_cjk.py

# Copy remaining project files
COPY sandbox/. .

# Ensure application files are writable by app user
RUN chown -R ubuntu:ubuntu /app

# Configure supervisor
COPY sandbox/supervisord.conf /etc/supervisor/conf.d/app.conf

# Expose ports
EXPOSE 8080 9222 5900 5901

ENV UVI_ARGS=""
ENV CHROME_ARGS=""

COPY --from=physical-reader-build /opt/dataseek-physical /opt/dataseek-physical

# Use supervisor to start all services
CMD ["supervisord", "-n", "-c", "/app/supervisord.conf"] 
