# syntax=docker/dockerfile:1
#
# Egress proxy for install containers (SPEC §9.3 Network, P2-08). The build context is the
# proxy package, so the image holds nothing but the standard-library server:
#
#   docker build -f images/proxy/Dockerfile -t mpilot/proxy:1 src/mpilot/sandbox/proxy
#
# Same pinned base as the python 3.12 sandbox image (looked up 2026-11-21, see that file).
# No package manager, compiler or shell tools are added; the server needs none.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS base-1

# WORKDIR first: it creates the directory with normal modes (a COPY --chmod would give the
# directory the file's mode, and 0444 leaves it untraversable).
WORKDIR /opt/egress-proxy
COPY --chmod=0444 server.py /opt/egress-proxy/server.py

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
# nobody:nogroup. The proxy needs no file it can write; its root is read-only at run time.
USER 65534:65534
ENTRYPOINT ["python", "-I", "-S", "/opt/egress-proxy/server.py"]
