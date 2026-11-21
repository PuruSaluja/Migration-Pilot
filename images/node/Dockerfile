# syntax=docker/dockerfile:1
#
# Sandbox base image, Node.js (SPEC §9.3). Build one major per image:
#
#   docker build --build-arg NODE_MAJOR=20 -t mpilot/node:20 images/node
#
# Every FROM line is pinned by the digest of the multi-arch image index, looked up from the
# registry (`docker buildx imagetools inspect <tag>`) on 2026-11-21; never typed from memory.
# To refresh a pin, re-run that command for its tag and replace the digest here and in
# images/digests.lock (P2-07). BuildKit pulls only the stage NODE_MAJOR selects.
ARG NODE_MAJOR=22

FROM node:18-bookworm-slim@sha256:f9ab18e354e6855ae56ef2b290dd225c1e51a564f87584b9bd21dd651838830e AS base-18
FROM node:20-bookworm-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS base-20
FROM node:22-bookworm-slim@sha256:e9f565d32859f0cbfb52125eafe8be39390f24dac302dc3cdf566e8ba3ab21ee AS base-22

FROM base-${NODE_MAJOR} AS final

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
    # The official node image ships a `node` user at uid 1000; replace it with the sandbox user.
    userdel -r node; \
    groupadd --gid "${SANDBOX_GID}" sandbox; \
    useradd --uid "${SANDBOX_UID}" --gid "${SANDBOX_GID}" --no-create-home \
        --home-dir /tmp --shell /bin/bash sandbox; \
    mkdir /workspace; \
    chown "${SANDBOX_UID}:${SANDBOX_GID}" /workspace

USER sandbox
WORKDIR /workspace
CMD ["sleep", "infinity"]
