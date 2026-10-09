# syntax=docker/dockerfile:1
# OPENCBM_SOURCE=git builds libopencbm and the xum1541 plugin (bounded waits, X
# protocol) from OPENCBM_REPO at OPENCBM_REF; OPENCBM_SOURCE=image uses OPENCBM_IMAGE.
ARG OPENCBM_SOURCE=git
ARG OPENCBM_IMAGE=anarkiwi/opencbm:latest
ARG OPENCBM_REPO=https://github.com/anarkiwi/OpenCBM
ARG OPENCBM_REF=1617823447e3b4d663cb058dd74b0aa17d579703

FROM ubuntu:26.04 AS drivecode
RUN apt-get update && apt-get install -y --no-install-recommends cc65 make \
    && rm -rf /var/lib/apt/lists/*
COPY drive/ /src/drive/
RUN mkdir -p /out && make -C /src/drive OUT=/out

FROM ubuntu:24.04 AS opencbm-build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential ca-certificates cc65 git libncurses-dev libusb-1.0-0-dev \
        pkg-config \
    && rm -rf /var/lib/apt/lists/*
ARG OPENCBM_REPO
ARG OPENCBM_REF
RUN git init -q /src \
    && git -C /src fetch -q --depth 1 "${OPENCBM_REPO}" "${OPENCBM_REF}" \
    && git -C /src checkout -q FETCH_HEAD
WORKDIR /src
RUN make -f LINUX/Makefile opencbm plugin-xum1541 \
    && make -f LINUX/Makefile DESTDIR=/out install install-plugin-xum1541

FROM ubuntu:24.04 AS opencbm-git
RUN apt-get update && apt-get install -y --no-install-recommends libusb-1.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=opencbm-build /out/usr/local/bin/ /usr/local/bin/
COPY --from=opencbm-build /out/usr/local/lib/ /usr/local/lib/
COPY --from=opencbm-build /out/etc/opencbm.conf /etc/opencbm.conf
COPY --from=opencbm-build /out/etc/opencbm.conf.d/ /etc/opencbm.conf.d/
COPY --from=opencbm-build /out/etc/udev/rules.d/ /etc/udev/rules.d/
RUN echo /usr/local/lib > /etc/ld.so.conf.d/opencbm.conf && ldconfig

FROM ${OPENCBM_IMAGE} AS opencbm-image

# Python dependencies are resolved on plain Ubuntu so an OpenCBM change keeps them cached.
FROM ubuntu:24.04 AS pydeps
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /venv
ENV PATH=/venv/bin:$PATH
COPY pyproject.toml /tmp/
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;print(" ".join(tomllib.load(open("/tmp/pyproject.toml","rb"))["project"]["dependencies"]))')

FROM pydeps AS pydev
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;print(" ".join(tomllib.load(open("/tmp/pyproject.toml","rb"))["project"]["optional-dependencies"]["dev"]))')

FROM opencbm-${OPENCBM_SOURCE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
ENV PATH=/venv/bin:$PATH \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
    NUMBA_CACHE_DIR=/tmp/numba-cache
WORKDIR /app

FROM base AS test
COPY --from=pydev /venv /venv
COPY --from=drivecode /out/ /opt/nybulah/drivecode/
ENV NYBULAH_DRIVECODE=/opt/nybulah/drivecode
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps -e .

FROM base AS runtime
COPY --from=pydeps /venv /venv
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps . && rm -rf /app
WORKDIR /data
ENTRYPOINT ["nybulah"]
