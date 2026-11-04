"""Small synthetic compressed copies of already-sanitized Codex test data."""
import sys

# Complete container with an intentionally invalid compressed block (RFC 8878).
BAD_FRAME = b'\x28\xb5\x2f\xfd\x20\x01\x0d\x00\x00\xff'


def compress(data, *, content_size=True, checksum=False):
    if sys.version_info >= (3, 14):
        from compression import zstd
        return zstd.compress(data, options={zstd.CompressionParameter.compression_level: 3,
                                                   zstd.CompressionParameter.checksum_flag: int(checksum),
                                                   zstd.CompressionParameter.content_size_flag: int(content_size)})
    import zstandard
    return zstandard.ZstdCompressor(level=3, write_checksum=checksum, write_content_size=content_size).compress(data)
