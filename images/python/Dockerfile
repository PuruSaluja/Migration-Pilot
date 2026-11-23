# syntax=docker/dockerfile:1
#
# Sandbox base image, Python (SPEC §9.3). Build one version per image:
#
#   docker build --build-arg PYTHON_VERSION=3.12 -t mpilot/python:3.12 images/python
#
# Every FROM line is pinned by the digest of the multi-arch image index, looked up from the
# registry (`docker buildx imagetools inspect <tag>`) on 2026-11-21; never typed from memory.
# To refresh a pin, re-run that command for its tag and replace the digest here and in
# images/digests.lock (P2-07). BuildKit pulls only the stage PYTHON_VERSION selects.
ARG PYTHON_VERSION=3.12

FROM python:3.9-slim-bookworm@sha256:a02e9c5406c416c504d6c9a1a306ff4080c3173f1008d192f953bd20382a2d5c AS base-3.9
FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 AS base-3.10
FROM python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39 AS base-3.11
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base-3.12
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base-3.13

FROM base-${PYTHON_VERSION} AS final

# tree-sitter CLI: a pinned GitHub release binary, checked against its published SHA-256.
# (Image builds may reach GitHub release tarballs; running containers never do.)
ARG TARGETARCH
ARG TREE_SITTER_VERSION=v0.25.10
ARG TREE_SITTER_SHA256_AMD64=8283ddba69253c698f6e987ba0e2f9285e079c8db4d36ebe1394b5bb3a0ebdfd
ARG TREE_SITTER_SHA256_ARM64=07fbff8ae0eeb0d3e496e14fc1a30dcc730cc2c97d70e601e5357f2e51958af5

# Package managers (P2-07): pip ships with the base image. uv is a pinned release binary
# checked against its published SHA-256; Poetry and PDM are pinned PyPI releases, each in its
# own venv under /opt/tools so their dependencies never touch the repo's environment.
# PDM is installed with its `locked` extra, which pins every dependency to the versions it was
# tested with: without it pip picks the newest hishel, whose API PDM 2.26 does not know
# (`pdm lock` and `pdm install` crash with "no attribute SyncSqliteStorage"; found in P2-11).
# Its update check cannot work offline and only prints a notice, so it is off.
ARG UV_VERSION=0.13.9
ARG UV_SHA256_AMD64=a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5
ARG UV_SHA256_ARM64=0797542018e541a9288855df555c8077eed06f7a47d206362030480e8ecc9f3d
ARG POETRY_VERSION=2.2.1
ARG PDM_VERSION=2.26.9
# pyright (P2-11b): the PyPI wrapper's `nodejs` extra brings Node from a wheel instead of
# downloading it on first use, so the type check runs with no network at all.
ARG PYRIGHT_VERSION=1.1.414
ENV PDM_CHECK_UPDATE=false

# The sandbox user's uid must equal the host user's uid (§9.3); the driver also passes
# --user uid:gid at run time, so this build arg only has to match for a clean /workspace owner.
ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) ts_arch=x64;   ts_sha="${TREE_SITTER_SHA256_AMD64}"; \
               uv_arch=x86_64;  uv_sha="${UV_SHA256_AMD64}" ;; \
        arm64) ts_arch=arm64; ts_sha="${TREE_SITTER_SHA256_ARM64}"; \
               uv_arch=aarch64; uv_sha="${UV_SHA256_ARM64}" ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git ripgrep; \
    curl -fsSL --retry 3 -o /tmp/tree-sitter.gz \
        "https://github.com/tree-sitter/tree-sitter/releases/download/${TREE_SITTER_VERSION}/tree-sitter-linux-${ts_arch}.gz"; \
    echo "${ts_sha}  /tmp/tree-sitter.gz" | sha256sum -c -; \
    gunzip -c /tmp/tree-sitter.gz > /usr/local/bin/tree-sitter; \
    chmod 0755 /usr/local/bin/tree-sitter; \
    rm -f /tmp/tree-sitter.gz; \
    curl -fsSL --retry 3 -o /tmp/uv.tar.gz \
        "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${uv_arch}-unknown-linux-gnu.tar.gz"; \
    echo "${uv_sha}  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /tmp "uv-${uv_arch}-unknown-linux-gnu/uv"; \
    install -m 0755 "/tmp/uv-${uv_arch}-unknown-linux-gnu/uv" /usr/local/bin/uv; \
    rm -rf /tmp/uv.tar.gz "/tmp/uv-${uv_arch}-unknown-linux-gnu"; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    for tool in poetry pdm pyright; do \
        python -m venv "/opt/tools/${tool}"; \
        spec="$(case ${tool} in poetry) echo "poetry==${POETRY_VERSION}";; pdm) echo "pdm[locked]==${PDM_VERSION}";; pyright) echo "pyright[nodejs]==${PYRIGHT_VERSION}";; esac)"; \
        "/opt/tools/${tool}/bin/pip" install --no-cache-dir --disable-pip-version-check "${spec}"; \
        ln -s "/opt/tools/${tool}/bin/${tool}" "/usr/local/bin/${tool}"; \
    done; \
    groupadd --gid "${SANDBOX_GID}" sandbox; \
    useradd --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" --no-create-home \
        --home-dir /tmp --shell /bin/bash sandbox; \
    mkdir /workspace; \
    chown "${SANDBOX_UID}:${SANDBOX_GID}" /workspace

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
