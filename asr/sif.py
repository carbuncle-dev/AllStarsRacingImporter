"""Reader for Sumo .sif chunk containers (the decompressed form of .zif).

Chunks are laid out back to back, each with a 16-byte header:
    char[4] tag, u32 chunk_size (padded), u32 data_size, u32 0x44332211
followed by data. Every data chunk is followed by a 'RELO' chunk listing the
pointer fields inside it; pointers are already offsets from the start of the
chunk data, so readers can follow them directly and RELO chunks are skipped.

Track tags: TRAK (navigation), FORE (forest/meshes), PVD4 (visibility),
COLI (collision), LOGC (logic).
"""

from __future__ import annotations

import struct
from pathlib import Path


def read_sif(path: Path) -> dict[str, bytes]:
    buf = Path(path).read_bytes()
    chunks: dict[str, bytes] = {}
    off = 0
    while off + 16 <= len(buf):
        tag, chunk_size, data_size, _ = struct.unpack_from("<4sIII", buf, off)
        if chunk_size == 0:
            break
        if tag != b"RELO":
            chunks.setdefault(tag.decode("ascii"), buf[off + 16:off + 16 + data_size])
        off += chunk_size
    return chunks
