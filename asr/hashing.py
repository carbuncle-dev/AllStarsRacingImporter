"""Sumo Digital string hashes (ported from ennuo/SlMod SlUtil.cs)."""

M32 = 0xFFFFFFFF


def fnv_hash(s: str) -> int:
    """SlUtil.HashString: FNV-1 variant (multiply, then xor)."""
    h = 0x811C9DC5
    for ch in s:
        h = ((h * 0x1000193) & M32) ^ ord(ch)
    return h


def _mix(a: int, b: int, c: int) -> tuple[int, int, int]:
    a = (a - b - c) & M32; a ^= c >> 13
    b = (b - c - a) & M32; b ^= (a << 8) & M32
    c = (c - a - b) & M32; c ^= b >> 13
    a = (a - b - c) & M32; a ^= c >> 12
    b = (b - c - a) & M32; b ^= (a << 16) & M32
    c = (c - a - b) & M32; c ^= b >> 5
    a = (a - b - c) & M32; a ^= c >> 3
    b = (b - c - a) & M32; b ^= (a << 10) & M32
    c = (c - a - b) & M32; c ^= b >> 15
    return a, b, c


def sumo_hash(s: str | bytes) -> int:
    """SlUtil.SumoHash: Bob Jenkins' lookup2 with initval 0x04C11DB7."""
    k = s.encode("ascii") if isinstance(s, str) else s
    a = b = 0x9E3779B9
    c = 0x04C11DB7
    off, n = 0, len(k)
    while n >= 12:
        a = (a + int.from_bytes(k[off:off + 4], "little")) & M32
        b = (b + int.from_bytes(k[off + 4:off + 8], "little")) & M32
        c = (c + int.from_bytes(k[off + 8:off + 12], "little")) & M32
        a, b, c = _mix(a, b, c)
        off += 12
        n -= 12
    c = (c + len(k)) & M32
    tail = k[off:]
    # Bytes 8..10 go to c shifted by 8 (low byte of c is reserved for the length).
    for i, byte in enumerate(tail):
        if i < 4:
            a = (a + (byte << (8 * i))) & M32
        elif i < 8:
            b = (b + (byte << (8 * (i - 4)))) & M32
        else:
            c = (c + (byte << (8 * (i - 7)))) & M32
    return _mix(a, b, c)[2]


def signed(h: int) -> int:
    return h - (1 << 32) if h & 0x80000000 else h
