"""Token estimate without a tokenizer (B3, SPEC §10.3).

Used wherever a token limit is enforced on text we build (repo map, Explorer answer,
migration notes, Diagnoser input). Deliberately conservative: `ceil(characters / 3)`.
Reported usage always comes from the runtime, never from this function.
"""

from __future__ import annotations

CHARS_PER_TOKEN = 3


def estimate_tokens(text: str) -> int:
    """Conservative token estimate: `ceil(len(text) / 3)`."""
    return -(-len(text) // CHARS_PER_TOKEN)
