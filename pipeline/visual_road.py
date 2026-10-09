"""The ASR track's visible geometry in CW collision space, for placing actors on what players see.

The collision mesh (what karts drive on) and the render mesh differ by centimetres to decimetres
(sleepers and rails stand above the collision surface), so actors snapped to the collision surface can
end up partly inside the visible track. Triangles come from forest_to_obj's OBJ (track.Forest objects,
see-through materials excluded), mapped like the meshes: CW = ASR @ R.T + OFF (align.json).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

SEE_THROUGH = ("waterfall", "fog", "cloud", "sky", "glow", "light", "water", "shadow")


def load(obj: Path, align: Path) -> np.ndarray:
    """(N, 3, 3) float64 triangles in CW space."""
    a = json.loads(align.read_text())
    rot, off = np.array(a["rotation"]), np.array(a["offset"])
    verts, tris = [], []
    use = False
    for line in open(obj, errors="replace"):
        if line.startswith("v "):
            verts.append(line.split()[1:4])
        elif line.startswith("o "):
            use = line[2:].strip().startswith("track.Forest")
        elif line.startswith("usemtl "):
            mat = line[7:].strip().lower()
            use_mat = not any(k in mat for k in SEE_THROUGH)
        elif line.startswith("f ") and use and use_mat:
            idx = [int(t.split("/")[0]) for t in line.split()[1:]]
            for i in range(1, len(idx) - 1):
                tris.append((idx[0], idx[i], idx[i + 1]))
    v = np.array(verts, np.float64) @ rot.T + off
    t = np.array(tris, np.int64)
    t = np.where(t > 0, t - 1, len(v) + t)  # OBJ indices are 1-based (negative: relative)
    return v[t]


def heights_above(tris: np.ndarray, points: np.ndarray, up: np.ndarray, window=(-0.3, 0.6)) -> np.ndarray:
    """Per point: the highest visible surface along `up` within `window` m of it (nan if none)."""
    a, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
    h = np.cross(up, e2)
    det = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(det) > 1e-9
    f = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    out = np.full(len(points), np.nan)
    for k, p in enumerate(points):
        sv = p - a
        u = f * np.einsum("ij,ij->i", sv, h)
        q = np.cross(sv, e1)
        v = f * (q @ up)
        t = f * np.einsum("ij,ij->i", e2, q)  # hit = p + up * t
        m = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t >= window[0]) & (t <= window[1])
        if m.any():
            out[k] = t[m].max()
    return out
