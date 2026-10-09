"""ASR track collision mesh (COLI chunk), layout from ennuo/SlMod CollisionMesh.cs.

Header (0x48 bytes):
    u32 name_hash, u32 version, u32 num_vertices, u32 num_triangles,
    u32 num_octree_nodes, u32 num_octree_indices, float3 min, float3 max,
    ptr vertices (float4[]), ptr triangles, ptr normals (float4[]),
    ptr octree_nodes, ptr octree_indices, ptr name
Triangle (0xC bytes): u16 v0, u16 v1, u16 v2, u16 flags, u32 surface (sumo_hash of name)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .hashing import sumo_hash

# Surface names recovered by hashing every string in the game's archives.
KNOWN_SURFACES = [
    "track_wooden_boards", "track_metal", "track_technometalmesh", "track_tileswater",
    "barrier_woodsplinters", "reset",
]
SURFACE_NAMES = {sumo_hash(n): n for n in KNOWN_SURFACES}


@dataclass
class CollisionMesh:
    name: str
    vertices: np.ndarray   # (N, 3) float32, game space (Y up)
    triangles: np.ndarray  # (M, 3) uint16 vertex indices
    flags: np.ndarray      # (M,) uint16
    surfaces: np.ndarray   # (M,) uint32 surface hashes

    def surface_name(self, h: int) -> str:
        return SURFACE_NAMES.get(int(h), f"surface_{int(h):08x}")


def parse_collision(data: bytes) -> CollisionMesh:
    hdr = np.frombuffer(data, dtype="<u4", count=18)
    nv, nt = int(hdr[2]), int(hdr[3])
    vp, tp, name_p = int(hdr[12]), int(hdr[13]), int(hdr[17])
    verts = np.frombuffer(data, dtype="<f4", count=nv * 4, offset=vp).reshape(nv, 4)[:, :3]
    tri = np.frombuffer(data, dtype=np.dtype([("v", "<u2", 3), ("flags", "<u2"), ("surface", "<u4")]),
                        count=nt, offset=tp)
    # The name is the last thing in the chunk and may lack a terminator.
    name = data[name_p:].split(b"\0", 1)[0].decode("ascii")
    return CollisionMesh(name, verts.copy(), tri["v"].copy(), tri["flags"].copy(), tri["surface"].copy())
