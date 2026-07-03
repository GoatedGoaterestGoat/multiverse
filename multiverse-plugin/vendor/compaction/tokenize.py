"""Token counting.

Text tokens use tiktoken's cl100k_base encoder when available (fast,
deterministic, close enough to Claude's tokenization for budget math),
falling back to a char/4 heuristic if tiktoken isn't installed. We do
NOT use the actual Claude tokenizer because (a) Anthropic ships only a
server-side counting endpoint, which would add latency to every leaf
parse, and (b) for compaction budget math a stable fast approximation
is fine — the agent's own context report uses the real tokenizer at the
API boundary.

Image tokens use Anthropic's documented formula: ``tokens ≈
(width × height) / 750``, with the longest side capped at 1568 pixels
(Anthropic resizes larger images down before token counting). See
``count_image_tokens`` below.
"""

from __future__ import annotations

import base64
import struct

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


# Anthropic's documented image sizing formula and its max-side cap.
_IMAGE_TOKENS_DIVISOR = 750
_IMAGE_MAX_SIDE = 1568
# Fallback estimate for images whose dimensions we can't determine
# (url / file_id source, unsupported format, or parse failure). Chosen
# to match a typical medium-sized screenshot.
_IMAGE_FALLBACK_TOKENS = 1600


def count_image_tokens(block: dict) -> int:
    """Estimate the token cost an image content block adds to context.

    Anthropic bills images (Claude 3+, all 4.x lines) at approximately
    ``(width × height) / 750`` tokens. Images larger than 1568 pixels on
    their longest side are resized down first, so we clamp before
    dividing.

    Concrete calibration points (matching Anthropic's docs):
    - 1092 × 1092 (typical) ≈ 1590 tokens
    - 1568 × 1568 (max) ≈ 3278 tokens
    - 512 × 512 (small) ≈ 350 tokens

    ``block`` is an Anthropic image block:
    ``{"type": "image", "source": {"type": "base64", "media_type": ...,
       "data": "<b64>"}}``. Only base64 sources carry inline bytes we
    can size; url/file-id sources fall back to
    ``_IMAGE_FALLBACK_TOKENS`` (a typical-screenshot estimate).

    Historical note: an earlier version of this function sized images
    by ``len(base64_data) / 1.44`` — treating the base64 string as if
    it were pasted-in text. That over-counted a typical screenshot by
    ~40× (a 162 KB JPEG was billed as ~112k tokens instead of ~2760)
    and made image-heavy sessions appear to consume many times their
    actual context. The current formula matches Anthropic's own
    reported usage.
    """
    if not isinstance(block, dict):
        return 0
    source = block.get("source")
    if not isinstance(source, dict):
        return 0
    data = source.get("data")
    if isinstance(data, str) and data:
        dims = _decode_image_dims_from_b64(data)
        if dims is not None:
            w, h = dims
            if w <= 0 or h <= 0:
                return _IMAGE_FALLBACK_TOKENS
            longest = max(w, h)
            if longest > _IMAGE_MAX_SIDE:
                scale = _IMAGE_MAX_SIDE / longest
                w = int(round(w * scale))
                h = int(round(h * scale))
            return max(1, (w * h) // _IMAGE_TOKENS_DIVISOR)
    # url / file_id source, or parse failure: use the fallback.
    return _IMAGE_FALLBACK_TOKENS


# ---------------------------------------------------------------------------
# Image-dimension sniffing (JPEG / PNG / WebP header parsers)
# ---------------------------------------------------------------------------

def _b64_prefix_bytes(data_b64: str, n_chars: int) -> bytes:
    """Base64-decode roughly the first ``n_chars`` chars of ``data_b64``,
    padding as needed. Silently returns ``b""`` if the prefix is
    unparseable (which we treat as "unknown format, use fallback")."""
    if not data_b64:
        return b""
    n = max(4, n_chars)
    prefix = data_b64[:n]
    # Trim to a multiple of 4 chars for valid base64 alignment.
    prefix = prefix[: len(prefix) - (len(prefix) % 4)]
    if not prefix:
        return b""
    try:
        return base64.b64decode(prefix, validate=False)
    except (ValueError, base64.binascii.Error):
        return b""


def _decode_image_dims_from_b64(data_b64: str) -> tuple[int, int] | None:
    """Return ``(width, height)`` for a base64-encoded image, sniffing
    from the file header only. Recognizes PNG, JPEG, and WebP —
    returns ``None`` for anything else.
    """
    # 24 raw bytes is enough for PNG signature + IHDR, plus WebP header;
    # JPEG SOFn scan needs more (screens can push it a few segments in),
    # so decode a larger prefix once and reuse.
    header = _b64_prefix_bytes(data_b64, 6000)
    if len(header) < 24:
        return None

    # PNG: 8-byte signature, then an IHDR chunk. Width is at offset 16..20,
    # height at 20..24 (both big-endian 32-bit unsigned).
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            w, h = struct.unpack(">II", header[16:24])
            return (w, h)
        except struct.error:
            return None

    # JPEG: SOI marker FF D8, then a sequence of segments. Look for a
    # Start-Of-Frame marker (0xC0..0xCF, minus non-SOF ones in that range).
    if header[:2] == b"\xff\xd8":
        return _jpeg_dims(header)

    # WebP (RIFF): 'RIFF' at 0..4, 'WEBP' at 8..12. Chunk type at 12..16:
    # 'VP8 ' (lossy), 'VP8L' (lossless), or 'VP8X' (extended).
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return _webp_dims(header)

    return None


# --- JPEG ------------------------------------------------------------------

_JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
})
_JPEG_STANDALONE_MARKERS = frozenset({
    0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7,  # RST0..RST7
    0xD8,  # SOI (won't appear again after the first)
    0xD9,  # EOI
    0x01,  # TEM
})


def _jpeg_dims(buf: bytes) -> tuple[int, int] | None:
    i = 2  # skip SOI (FF D8)
    n = len(buf)
    while i < n - 8:
        # Sync to next marker byte (should always be 0xFF).
        if buf[i] != 0xFF:
            i += 1
            continue
        # Fill bytes: any run of 0xFF between real markers.
        while i < n and buf[i] == 0xFF:
            i += 1
        if i >= n:
            return None
        marker = buf[i]
        i += 1
        if marker in _JPEG_SOF_MARKERS:
            # Segment: len(2) precision(1) height(2) width(2) ...
            if i + 7 > n:
                return None
            try:
                h, w = struct.unpack(">HH", buf[i + 3:i + 7])
                return (w, h)
            except struct.error:
                return None
        if marker in _JPEG_STANDALONE_MARKERS:
            # No length-carrying payload.
            continue
        # Length-carrying segment: skip len bytes total.
        if i + 2 > n:
            return None
        try:
            seg_len = struct.unpack(">H", buf[i:i + 2])[0]
        except struct.error:
            return None
        if seg_len < 2:
            return None
        i += seg_len
    return None


# --- WebP ------------------------------------------------------------------

def _webp_dims(buf: bytes) -> tuple[int, int] | None:
    if len(buf) < 30:
        return None
    chunk_type = buf[12:16]
    if chunk_type == b"VP8X":
        # VP8X: at offset 24..27 (LE 24-bit) is width-1, then height-1.
        if len(buf) < 30:
            return None
        w = buf[24] | (buf[25] << 8) | (buf[26] << 16)
        h = buf[27] | (buf[28] << 8) | (buf[29] << 16)
        return (w + 1, h + 1)
    if chunk_type == b"VP8 ":
        # VP8 lossy: after 20-byte header + 3-byte start code, 2 bytes for
        # width (14 low bits) and 2 for height. Positioned at offsets
        # 26..30 in the RIFF stream.
        if len(buf) < 30:
            return None
        try:
            w = struct.unpack("<H", buf[26:28])[0] & 0x3FFF
            h = struct.unpack("<H", buf[28:30])[0] & 0x3FFF
            return (w, h)
        except struct.error:
            return None
    if chunk_type == b"VP8L":
        # VP8L: after signature byte at 20, 4 bytes carry width-1 (14b) and
        # height-1 (14b) packed LE.
        if len(buf) < 25:
            return None
        b0, b1, b2, b3 = buf[21], buf[22], buf[23], buf[24]
        w = ((b1 & 0x3F) << 8 | b0) + 1
        h = ((b3 & 0x0F) << 10 | b2 << 2 | (b1 & 0xC0) >> 6) + 1
        return (w, h)
    return None
