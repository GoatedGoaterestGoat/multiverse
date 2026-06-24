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


def count_image_tokens(block: dict) -> int:
    """Estimate the token cost an image content block adds to context.

    Empirically, the backing model bills base64 image payloads close to
    their cost *as text*: a 991,876-char base64 corpus across 6 images
    measured 689,712 real cl100k tokens — a steady ~1.44 chars/token, much
    denser than ordinary prose because base64 is high-entropy and few BPE
    merges apply. Adding that to the text count matched the model's own
    reported usage to within ~2%. So we size an image as
    ``len(base64_data) / 1.44``.

    We use this fixed base64 ratio rather than routing through
    `count_tokens`: the char/4 fallback (active whenever tiktoken isn't
    installed — which is the default here, tiktoken is not a dependency)
    under-counts dense base64 by ~2.5x, and even tiktoken's cl100k differs
    slightly from this model's tokenizer. The measured ratio is the most
    accurate single constant for this payload class.

    This deliberately does NOT use a fixed per-image constant (e.g. ~1600
    tokens, the dimension-based scheme some providers use): on this model
    that under-counts a single large screenshot by ~200x and is what made
    the dashboard report 280k for a 990k-token session. If a future model
    bills images by a flat/dimension rule instead, swap this heuristic.

    `block` is an Anthropic image block: ``{"type":"image","source":{...}}``.
    Only base64 sources carry inline bytes we can size; url/file-id sources
    fall back to a conservative flat estimate.
    """
    if not isinstance(block, dict):
        return 0
    source = block.get("source")
    if not isinstance(source, dict):
        return 0
    data = source.get("data")
    if isinstance(data, str) and data:
        # Measured ~1.44 base64 chars per token on this model.
        return max(1, int(len(data) / 1.44))
    # Non-base64 source (url / file id): no inline bytes to size. Use a flat
    # placeholder so it isn't counted as zero.
    return 1600
