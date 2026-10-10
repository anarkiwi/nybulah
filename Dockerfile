# OPENCBM_SOURCE=git builds libopencbm and the xum1541 plugin (bounded waits, X
# protocol) from OPENCBM_REPO at OPENCBM_REF; OPENCBM_SOURCE=local builds them from
# the build context named opencbm (--build-context opencbm=<OpenCBM tree>);
# OPENCBM_SOURCE=image uses OPENCBM_IMAGE.
ARG OPENCBM_SOURCE=git
ARG OPENCBM_IMAGE=anarkiwi/opencbm:latest
ARG OPENCBM_REPO=https://github.com/anarkiwi/OpenCBM
ARG OPENCBM_REF=ab77214319da1c5469c46c9e9d9628e241272a01

FROM ubuntu:26.04@sha256:f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7 AS drivecode
RUN apt-get update && apt-get install -y --no-install-recommends cc65 make \
    && rm -rf /var/lib/apt/lists/*
COPY drive/ /src/drive/
RUN mkdir -p /out && make -C /src/drive OUT=/out

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS opencbm-deps
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cc65 git libncurses-dev libusb-1.0-0-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*

FROM opencbm-deps AS opencbm-src-git
ARG OPENCBM_REPO
ARG OPENCBM_REF
RUN git init -q /src \
    && git -C /src fetch -q --depth 1 "${OPENCBM_REPO}" "${OPENCBM_REF}" \
    && git -C /src checkout -q FETCH_HEAD

FROM opencbm-deps AS opencbm-src-local
COPY --from=opencbm . /src

FROM opencbm-src-${OPENCBM_SOURCE} AS opencbm-build
WORKDIR /src
RUN make -f LINUX/Makefile opencbm plugin-xum1541 \
    && make -f LINUX/Makefile DESTDIR=/out install install-plugin-xum1541

FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS opencbm-git
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=opencbm-build /out/usr/local/bin/ /usr/local/bin/
COPY --from=opencbm-build /out/usr/local/lib/ /usr/local/lib/
COPY --from=opencbm-build /out/etc/opencbm.conf /etc/opencbm.conf
COPY --from=opencbm-build /out/etc/opencbm.conf.d/ /etc/opencbm.conf.d/
COPY --from=opencbm-build /out/etc/udev/rules.d/ /etc/udev/rules.d/
RUN echo /usr/local/lib > /etc/ld.so.conf.d/opencbm.conf && ldconfig

FROM opencbm-git AS opencbm-local

FROM ${OPENCBM_IMAGE} AS opencbm-image

# VICE (GPL, run as a separate program) built headless from a pinned release
# tarball; its bundled ROMs stay in the image (data/C64, C128, DRIVES).
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS vice-build
ARG VICE_VERSION=3.10
ARG VICE_SHA256=8e5bac18cbcb9f192380ad3ef881f8790f5b75c41d7b3da65d831985d864d6d1
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        bison build-essential ca-certificates curl dos2unix file flex pkg-config xa65 zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/vice.tar.gz \
        "https://downloads.sourceforge.net/project/vice-emu/releases/vice-${VICE_VERSION}.tar.gz" \
    && echo "${VICE_SHA256}  /tmp/vice.tar.gz" | sha256sum -c - \
    && tar -xzf /tmp/vice.tar.gz -C /tmp && rm /tmp/vice.tar.gz
WORKDIR /tmp/vice-${VICE_VERSION}
RUN ./configure --prefix=/opt/vice --enable-headlessui --disable-html-docs \
        --disable-pdf-docs --without-alsa --without-pulse --without-png \
        --without-flac --without-mpg123 --without-vorbis --without-lame \
        --without-portaudio --disable-ethernet --disable-realdevice --disable-midi \
        --disable-rs232 --disable-openmp --without-libcurl \
    && make -j"$(nproc)" -C src x64sc x128 c1541 \
    && mkdir -p /opt/vice/bin /opt/vice/share/vice \
    && cp src/x64sc src/x128 src/c1541 /opt/vice/bin/ \
    && cp -r data/C64 data/C128 data/DRIVES /opt/vice/share/vice/

FROM scratch AS vice
COPY --from=vice-build /opt/vice/ /opt/vice/

# Python dependencies are resolved on plain Ubuntu so an OpenCBM change keeps them cached.
FROM ubuntu:24.04@sha256:534baea6a22c03a63003dbc8dbe78fe34bc0d7e595d9a9dc9834884ff530eb55 AS pydeps
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
COPY pyproject.toml /tmp/
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;p=tomllib.load(open("/tmp/pyproject.toml","rb"))["project"];print(" ".join(p["dependencies"]+p["optional-dependencies"]["viz"]))')

FROM pydeps AS pydev
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;print(" ".join(tomllib.load(open("/tmp/pyproject.toml","rb"))["project"]["optional-dependencies"]["dev"]))')

FROM opencbm-${OPENCBM_SOURCE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/venv/bin:$PATH \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
    NUMBA_CACHE_DIR=/tmp/numba-cache MPLCONFIGDIR=/tmp/matplotlib
WORKDIR /app

FROM base AS test
COPY --from=pydev /venv /venv
COPY --from=drivecode /out/ /opt/nybulah/drivecode/
ENV NYBULAH_DRIVECODE=/opt/nybulah/drivecode
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps -e .
RUN python -c "from nybulah import simfast; simfast.warm()" \
    && chmod -R a+rwX "$NUMBA_CACHE_DIR"

FROM test AS test-vice
COPY --from=vice /opt/vice/ /opt/vice/
ENV PATH=/opt/vice/bin:$PATH NYBULAH_VICE_DATA=/opt/vice/share/vice

FROM base AS runtime
COPY --from=pydeps /venv /venv
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps . && rm -rf /app
WORKDIR /data
ENTRYPOINT ["nybulah"]
