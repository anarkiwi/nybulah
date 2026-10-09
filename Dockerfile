# syntax=docker/dockerfile:1
ARG OPENCBM_IMAGE=anarkiwi/opencbm:latest

FROM ubuntu:26.04 AS drivecode
RUN apt-get update && apt-get install -y --no-install-recommends cc65 make \
    && rm -rf /var/lib/apt/lists/*
COPY drive/ /src/drive/
RUN mkdir -p /out && make -C /src/drive OUT=/out

FROM ${OPENCBM_IMAGE} AS base
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /venv
ENV PATH=/venv/bin:$PATH \
    OMP_NUM_THREADS=2 MKL_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;print(" ".join(tomllib.load(open("pyproject.toml","rb"))["project"]["dependencies"]))')

FROM base AS test
RUN pip install --no-cache-dir $(python3 -c 'import tomllib;print(" ".join(tomllib.load(open("pyproject.toml","rb"))["project"]["optional-dependencies"]["dev"]))')
COPY --from=drivecode /out/ /opt/nybulah/drivecode/
ENV NYBULAH_DRIVECODE=/opt/nybulah/drivecode
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps -e .

FROM base AS runtime
COPY . .
COPY --from=drivecode /out/ nybulah/drivecode/
RUN pip install --no-cache-dir --no-deps . && rm -rf /app
WORKDIR /data
ENTRYPOINT ["nybulah"]
