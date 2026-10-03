# Kev decision model server (M7/M19): TypeSafe API on port 8008.
#
# Pinned as in docs/software-architecture.md §6: the repository at KEV_COMMIT, the weights at
# KEV_REVISION (release 2026-09-24), flash-linear-attention installed explicitly (missing from
# the repository's lock file). The weights are downloaded at build time, so the container runs
# offline on the internal network. The same image runs on CUDA when a GPU is attached
# (docker-compose.gpu.yml) and on CPU otherwise.
FROM python:3.12-slim

ARG KEV_REPO=https://github.com/jaredpalmer/kev.git
ARG KEV_COMMIT=0fe8fc97c2bcc247fa3efb6e5c32af4e99770e91
ARG KEV_MODEL=jaredpalmer/kev-0.8b
ARG KEV_REVISION=9a45d25eb2ab761841196625383fa1dff0e56c1e
ARG KEV_RELEASE_DATE=2026-09-24
ARG UV_VERSION=0.12.21
ARG FLA_VERSION=0.5.2

# A C/C++ toolchain: Triton and TorchInductor compile kernels at run time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==${UV_VERSION}"

ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    HF_HOME=/opt/hf \
    PYTHONUNBUFFERED=1

WORKDIR /kev
RUN git init -q . \
    && git remote add origin "${KEV_REPO}" \
    && git fetch -q --depth 1 origin "${KEV_COMMIT}" \
    && git checkout -q FETCH_HEAD \
    && rm -rf .git

# The repository's .python-version selects Python 3.13 (uv installs it).
RUN uv sync --frozen --extra serve \
    && uv pip install --python .venv/bin/python "flash-linear-attention==${FLA_VERSION}"

# The pinned checkpoint and its base model, downloaded into HF_HOME.
ENV KEV_RUN=${KEV_MODEL}@${KEV_REVISION}
COPY prefetch.py /usr/local/bin/kev-prefetch.py
RUN .venv/bin/python /usr/local/bin/kev-prefetch.py "${KEV_RUN}" "${KEV_RELEASE_DATE}"

COPY entrypoint.py /usr/local/bin/kev-entrypoint.py

ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    KEV_PORT=8008

EXPOSE 8008
# Healthy only after the warm-up (healthcheck in docker-compose.yml: kev-entrypoint.py
# --healthcheck).
ENTRYPOINT ["/kev/.venv/bin/python", "/usr/local/bin/kev-entrypoint.py"]
