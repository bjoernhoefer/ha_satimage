# syntax=docker/dockerfile:1

# Base image: mambaorg/micromamba instead of "python:3.12-slim" + pip.
#
# Rationale (see also HISTORY.md): the Satpy rendering chain needs
# pyresample, pykdtree and pyproj. On ARM64 (e.g. Raspberry Pi 5) these
# packages compile very slowly from the PyPI sdist (pykdtree/pyresample
# build native extensions, pyproj ships PROJ as a C library) and need extra
# system headers that slim images lack. conda-forge provides ready-made
# linux-aarch64 builds for all of them, so micromamba (a lean, statically
# linked conda replacement without the Anaconda overhead) speeds up the
# ARM64 build drastically and makes it reproducible.
#
# Current state: PyPI now ships manylinux-aarch64 wheels for all these
# packages, while conda-forge has no current linux-aarch64 build of
# trollimage (a Satpy dependency). The Satpy chain is therefore installed
# via pip from wheels; conda-forge only provides Python itself.
FROM mambaorg/micromamba:1.5-bookworm

ARG VERSION=dev
LABEL org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.source=https://github.com/bjoernhoefer/ha_satimage \
      org.opencontainers.image.licenses=MIT

USER root
WORKDIR /app

ENV MAMBA_DOCKERFILE_ACTIVATE=1 \
    PYTHONUNBUFFERED=1 \
    HA_SATIMAGE_CONFIG=/data/config.yaml \
    SATPY_DATA_DIR=/data/satpy \
    XDG_CACHE_HOME=/data/cache/xdg \
    TMPDIR=/data/tmp \
    HA_SATIMAGE_STDOUT_LOG_LEVEL=WARNING \
    MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=1 \
    OMP_THREAD_LIMIT=1 \
    OPENBLAS_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 \
    NUMEXPR_MAX_THREADS=1 \
    VECLIB_MAXIMUM_THREADS=1 \
    BLIS_NUM_THREADS=1

RUN micromamba install -y -n base -c conda-forge python=3.12 pip && \
    micromamba clean --all --yes

COPY pyproject.toml README.md /app/
COPY src /app/src

# --only-binary: never compile from an sdist (extremely slow on ARM/under
# QEMU) - if a wheel is missing, the build should fail immediately.
RUN micromamba run -n base pip install --no-cache-dir \
        --only-binary=numpy,pyproj,pyresample,pykdtree,trollimage,pillow,netCDF4,cftime .

# Healthchecks and `docker exec` bypass the entrypoint, i.e. the conda
# environment is not activated there and `python3` would not be on PATH.
RUN ln -sf /opt/conda/bin/python /usr/local/bin/python3

# Bytecode is compiled at build time, so imports never write .pyc files into
# the container layer (the system disk) at runtime.
RUN micromamba run -n base python -m compileall -q /opt/conda/lib/python3.12/site-packages || true
ENV PYTHONDONTWRITEBYTECODE=1

# No /data in the image and no VOLUME: without the bind mount of the host
# data directory the container must fail (storage check in the entrypoint),
# never write into an anonymous volume in Docker's data directory.

COPY docker/entrypoint.sh /usr/local/bin/ha_satimage-entrypoint.sh
RUN chmod +x /usr/local/bin/ha_satimage-entrypoint.sh

EXPOSE 6060

# Every probe is a process start plus a write of Docker's container state
# (Docker data directory, on the Raspberry Pi the SD card): every 2 min,
# with quick probes only during start-up (start-interval, Docker >= 25).
HEALTHCHECK --interval=2m --timeout=10s --retries=3 --start-period=2m --start-interval=5s \
    CMD /usr/local/bin/python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:6060/healthz')"

ENTRYPOINT ["/usr/local/bin/ha_satimage-entrypoint.sh"]
# No access log: UI polling would write a line per request to Docker's log.
CMD ["uvicorn", "ha_satimage.main:app", "--host", "0.0.0.0", "--port", "6060", "--no-access-log"]
