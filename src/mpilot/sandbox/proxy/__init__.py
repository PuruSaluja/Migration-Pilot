"""The egress proxy for install containers (SPEC §9.3 Network, decisions A7).

`server.py` is the proxy itself. It runs inside its own container, so it imports nothing but
the standard library (a test enforces that); the host-side pieces (image, container, log
parsing) come with P2-08b.
"""
