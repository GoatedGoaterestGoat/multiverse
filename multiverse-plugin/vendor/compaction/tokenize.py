"""Token counting.

Uses tiktoken's cl100k_base encoder when available (fast, deterministic,
close enough to Claude's tokenization for budget math). Falls back to a
char/4 heuristic if tiktoken isn't installed, for development convenience.

We do NOT use the actual Claude tokenizer because (a) Anthropic ships only
a server-side counting endpoint, which would add latency to every leaf
parse, and (b) for compaction budget math we just need a stable, fast
approximation -- the agent's own context counts will use the real
tokenizer at the API boundary.
"""

from __future__ import annotations

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_ENC.encode(text, disallowed_special=()))

except ImportError:
    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)
