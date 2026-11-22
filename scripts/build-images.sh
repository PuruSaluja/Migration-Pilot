#!/usr/bin/env bash
# Build the sandbox base images, or check them against images/digests.lock (SPEC §9.3, NFR-5).
#
#   scripts/build-images.sh build [node:20 ...]   build all (or the named) images
#   scripts/build-images.sh lock                  build all, then rewrite images/digests.lock
#   scripts/build-images.sh check                 lock vs the Dockerfiles (no Docker needed)
#   scripts/build-images.sh verify [node:20 ...]  lock vs the images on this host
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run python -m mpilot.sandbox.images "$@"
