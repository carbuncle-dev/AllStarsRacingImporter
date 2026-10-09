"""Fit directional "fill" lights that reproduce a sky light's diffuse lighting (tools/ue_build_level.py).

A sky light ignores lighting channels, and a movable one lights every loaded level (it brightened the
travel-ring course loaded alongside ours). The fills light only lighting channel 0, the main course's.

The sky is a CrossWorlds HDRI cubemap, read from its cooked BC6H data (editor project copy). Lower
hemisphere black, as on Metal Harbor's sky light. For 800 random surface normals the target is
intensity * cosine-weighted radiance / pi; a zenith light, one aligned with the HDRI's sun (placed along
our sun's azimuth) and four at 25 degrees around are fitted per colour channel by non-negative least
squares (mean error ~4%).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

CUBEMAP = (Path(os.environ.get("UE_ENGINE_DIR", Path.home() / "UE5-SRCW" / "Windows")) / "UNION" / "Content"
           / "01_Union" / "Asset" / "Stage" / "Common" / "Texture" / "HDRI" / "T_Sky_Cumulus01_HDR.uexp")
LIGHTS = [("zenith", 90, 90), ("sun", 0, 50)] + [(f"ring{k}", 90 * k, 25) for k in range(4)]  # az from sun, el


def _cubemap(path: Path, mip: int = 4):
    import imagecodecs
    d = path.read_bytes()
    o = d.find(b"PF_BC6H\0") + 8
    _, mips = struct.unpack_from("<ii", d, o)
    o += 8
    for m in range(mips):
        n = max(1, 1024 >> m)
        size = max(1, (n + 3) // 4) ** 2 * 16 * 6
        o += 4
        if m == mip:
            data = d[o:o + size]
            break
        o += size + 12
    fs = n * n
    faces = [np.asarray(imagecodecs.bcn_decode(data[f * fs:(f + 1) * fs], 6, shape=(n, n, 3)), np.float64)
             for f in range(6)]
    c = (np.arange(n) + 0.5) / n * 2 - 1
    s, t = np.meshgrid(c, c)
    one = np.ones_like(s)
    axes = [(one, -t, -s), (-one, -t, s), (s, one, t), (s, -one, -t), (s, -t, one), (-s, -t, -one)]  # D3D order
    dirs, rad, w = [], [], []
    for f, a in enumerate(axes):
        v = np.stack(a, -1)
        r2 = (v ** 2).sum(-1)
        dirs.append((v / np.sqrt(r2)[..., None]).reshape(-1, 3))
        rad.append(faces[f].reshape(-1, 3))
        w.append((r2 ** -1.5).reshape(-1) * (2 / n) ** 2)  # solid angle per texel
    return np.concatenate(dirs), np.concatenate(rad), np.concatenate(w)


def _nnls(a, b, iterations=40000):
    x = np.zeros(a.shape[1])
    step = np.linalg.norm(a, 2) ** 2
    for _ in range(iterations):
        x = np.maximum(0, x - (a.T @ (a @ x - b)) / step)
    return x


def fit(intensity: float, path: Path = CUBEMAP) -> list[dict]:
    d, rad, w = _cubemap(path)
    rad[d[:, 2] < 0] = 0  # face 4 (+Z) is the sky overhead
    sun = d[np.argmax(rad.sum(1))]
    sun_az = np.degrees(np.arctan2(sun[1], sun[0]))
    normals = np.random.default_rng(0).normal(size=(800, 3))
    normals /= np.linalg.norm(normals, axis=1)[:, None]
    target = intensity * (np.clip(normals @ d.T, 0, None) * w) @ rad / np.pi

    def direction(az, el):
        a, e = np.radians(az + sun_az), np.radians(el)
        return np.array([np.cos(e) * np.cos(a), np.cos(e) * np.sin(a), np.sin(e)])

    u = np.array([direction(az, el) for _, az, el in LIGHTS])
    a = np.clip(normals @ u.T, 0, None) / np.pi
    rgb = np.stack([_nnls(a, target[:, ch]) for ch in range(3)], 1)
    err = np.abs(a @ rgb - target).mean() / target.mean()
    print(f"sky fill: {len(LIGHTS)} lights, mean error {err:.1%}")
    return [{"name": n, "az_from_sun": az, "elevation": el, "rgb": np.round(c, 3).tolist()}
            for (n, az, el), c in zip(LIGHTS, rgb)]
