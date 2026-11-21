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
ARG TREE_SITTER_VERSION=v0.27.2
ARG TREE_SITTER_SHA256_AMD64=7ed56d152c52437397b9fcaa43cf341f49933cecc1ca291843f0d347c6e2cb3a
ARG TREE_SITTER_SHA256_ARM64=34fcc5c4fa7bdd2058a2a45a03b6eb9a49b03a93b6d52ec750f99b1516d98d33

# The sandbox user's uid must equal the host user's uid (§9.3); the driver also passes
# --user uid:gid at run time, so this build arg only has to match for a clean /workspace owner.
ARG SANDBOX_UID=1000
ARG SANDBOX_GID=1000

RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) ts_arch=x64;   ts_sha="${TREE_SITTER_SHA256_AMD64}" ;; \
        arm64) ts_arch=arm64; ts_sha="${TREE_SITTER_SHA256_ARM64}" ;; \
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
    apt-get purge -y --auto-remove curl; \
    rm -rf /var/lib/apt/lists/*; \
    groupadd --gid "${SANDBOX_GID}" sandbox; \
    useradd --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" --no-create-home \
        --home-dir /tmp --shell /bin/bash sandbox; \
    mkdir /workspace; \
    chown "${SANDBOX_UID}:${SANDBOX_GID}" /workspace

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
