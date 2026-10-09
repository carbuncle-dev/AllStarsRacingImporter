"""CrossWorlds road collision (UUnionBtCollisionBinaryAsset.Parameter.Data).

Reverse-engineered layout (little-endian). Blob header:
    u32 magic 0xEC17AC07, u32 version (1), u32 hash, u32 chunk_count,
    u32 chunk_offsets[chunk_count], padded to 16 bytes
Each chunk is one Bullet-style indexed triangle mesh (btTriangleIndexVertexArray):
    u32 version (1), u32 chunk_size,
    u32 num_vertices, u32 vertex_stride (12), u32 vertex_offset, u32 vertex_type (0 = float)
    u32 num_materials, u32 material_stride (16), u32 material_offset
    u32 num_parts, u32 part_material_stride (4), u32 part_material_offset
    u32 unknown (2), u32 num_parts,
    num_parts x (u32 num_triangles, u32 index_stride (12), u32 index_offset, u32 index_type (2 = int32))
All offsets are relative to the chunk start.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = 0xEC17AC07


@dataclass
class MeshPart:
    triangles: np.ndarray  # (N, 3) int vertex indices
    material: int          # index into CollisionChunk.materials


@dataclass
class CollisionChunk:
    vertices: np.ndarray   # (N, 3) float32, Unreal space
    materials: list[bytes]  # 16-byte surface records (flag bitfields)
    parts: list[MeshPart]
    header: dict = field(default_factory=dict)


@dataclass
class Collision:
    version: int
    hash: int
    chunks: list[CollisionChunk]


def parse_chunk(c: bytes) -> CollisionChunk:
    u = lambda o: struct.unpack_from("<I", c, o)[0]
    version, size = u(0), u(4)
    nv, vstride, voff, vtype = struct.unpack_from("<4I", c, 8)
    n_mat, mat_stride, mat_off = struct.unpack_from("<3I", c, 24)
    n_map, map_stride, map_off = struct.unpack_from("<3I", c, 36)
    unk, n_parts = struct.unpack_from("<2I", c, 48)
    assert vtype == 0 and vstride == 12, (vtype, vstride)
    assert n_map == n_parts and mat_stride == 16 and map_stride == 4, (n_mat, n_map, n_parts)
    materials = [c[mat_off + i * 16:mat_off + i * 16 + 16] for i in range(n_mat)]
    verts = np.frombuffer(c, "<f4", nv * 3, voff).reshape(nv, 3)
    parts = []
    for i in range(n_parts):
        nt, istride, ioff, itype = struct.unpack_from("<4I", c, 56 + i * 16)
        assert itype == 2 and istride == 12, (itype, istride)
        tris = np.frombuffer(c, "<u4", nt * 3, ioff).reshape(nt, 3)
        parts.append(MeshPart(tris, u(map_off + i * 4)))
    return CollisionChunk(verts, materials, parts, {"version": version, "size": size, "unknown": unk})


def parse_collision(data: bytes) -> Collision:
    magic, version, h, n = struct.unpack_from("<4I", data, 0)
    if magic != MAGIC:
        raise ValueError(f"bad collision magic {magic:#x}")
    offsets = list(struct.unpack_from(f"<{n}I", data, 16))
    ends = offsets[1:] + [len(data)]
    return Collision(version, h, [parse_chunk(data[o:e]) for o, e in zip(offsets, ends)])


def _pad16(b: bytes) -> bytes:
    return b + b"\0" * (-len(b) % 16)


def write_chunk(chunk: CollisionChunk) -> bytes:
    """Sections in file order: header, vertices, part indices, materials, part->material map."""
    n_parts = len(chunk.parts)
    header_size = 56 + n_parts * 16
    vert_off = header_size
    body = bytearray(np.ascontiguousarray(chunk.vertices, "<f4").tobytes())
    part_descs = []
    for p in chunk.parts:
        part_descs.append(struct.pack("<4I", len(p.triangles), 12, header_size + len(body), 2))
        body += np.ascontiguousarray(p.triangles, "<u4").tobytes()
    mat_off = header_size + len(body)
    body += b"".join(chunk.materials)
    map_off = header_size + len(body)
    body += struct.pack(f"<{n_parts}I", *(p.material for p in chunk.parts))
    size = header_size + len(body)
    size += -size % 16
    head = struct.pack("<2I", chunk.header.get("version", 1), size)
    head += struct.pack("<4I", len(chunk.vertices), 12, vert_off, 0)
    head += struct.pack("<3I", len(chunk.materials), 16, mat_off)
    head += struct.pack("<3I", n_parts, 4, map_off)
    head += struct.pack("<2I", chunk.header.get("unknown", 2), n_parts)
    return _pad16(head + b"".join(part_descs) + bytes(body))


def write_collision(col: Collision) -> bytes:
    """Serialize; the header hash is CRC32 of everything after the 16-byte header."""
    import zlib
    n = len(col.chunks)
    table_size = len(_pad16(b"\0" * (16 + n * 4)))
    chunks = [write_chunk(c) for c in col.chunks]
    offsets, pos = [], table_size
    for c in chunks:
        offsets.append(pos)
        pos += len(c)
    rest = _pad16(struct.pack(f"<{n}I", *offsets))[: table_size - 16] + b"".join(chunks)
    return struct.pack("<4I", MAGIC, col.version, zlib.crc32(rest), n) + rest
