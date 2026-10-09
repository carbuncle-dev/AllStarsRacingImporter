"""Texture for the disc that closes Rocky Coaster's cloud sea (export_for_unreal.close_cloud_floor).

Tiling one of ASR's cloud textures across the disc showed an obvious repeating pattern (CLOUDS.TGA is a
side-on cloud wall). Instead this synthesises one large, non-repeating image from the opaque, top-down
lower part of CLOUDSFADE.TGA: patches taken at random offsets, randomly mirrored, laid on a jittered
grid and cross-faded with smooth weights ("texture bombing"). The disc maps it once (UV 0-1 across its
diameter), so nothing repeats. Its mean (linear) colour is matched to the rings' texture, CLOUDS.TGA;
the rings' baked lighting reaches the floor as their mean vertex colour (export_for_unreal).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

SOURCE = "CLOUDSFADE"
SOURCE_ROWS = (340, 512)  # fully opaque rows (alpha >= 250): the cloud sea seen from above
STRETCH = 2.3  # the source is seen at a grazing angle: undo its vertical foreshortening
SIZE = 4096
WINDOW = 0.55  # patch width as a share of the source width
PATCH = 1000  # output pixels per patch (4096 px over the ~1300 m disc: cloud puffs ~40 m)
SEED = 7


def synthesise(src: np.ndarray, size: int = SIZE, patch: int = PATCH, seed: int = SEED) -> np.ndarray:
    rng = np.random.default_rng(seed)
    h, w = src.shape[:2]
    sw = int(w * WINDOW)  # each patch is a window of the source at a random offset (no mirrored seams)
    scale = patch / sw
    big = np.asarray(Image.fromarray(src).resize((int(w * scale), int(h * STRETCH * scale)), Image.LANCZOS),
                     np.float64)
    ph, pw = big.shape[0], patch
    win_y = np.sin(np.linspace(0, np.pi, ph)) ** 2
    win_x = np.sin(np.linspace(0, np.pi, pw)) ** 2
    weight = (win_y[:, None] * win_x[None, :]) ** 2  # high power: crisp patches, short cross-fades
    acc = np.zeros((size + 2 * ph, size + 2 * pw, 3))
    wsum = np.full(acc.shape[:2], 1e-9)
    step_y, step_x = ph // 2, pw // 2
    for y in range(0, size + ph, step_y):
        for x in range(0, size + pw, step_x):
            ox = rng.integers(0, big.shape[1] - pw + 1)
            tile = big[:, ox:ox + pw]
            if rng.random() < 0.5:
                tile = tile[:, ::-1]
            jy, jx = rng.integers(-step_y // 3, step_y // 3 + 1), rng.integers(-step_x // 3, step_x // 3 + 1)
            y0, x0 = y + jy + ph // 2, x + jx + pw // 2
            if y0 < 0 or x0 < 0 or y0 + ph > acc.shape[0] or x0 + pw > acc.shape[1]:
                continue
            acc[y0:y0 + ph, x0:x0 + pw] += tile * weight[..., None]
            wsum[y0:y0 + ph, x0:x0 + pw] += weight
    out = acc / wsum[..., None]
    return out[ph:ph + size, pw:pw + size]


def _linear(c):
    c = np.asarray(c, np.float64) / 255
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _srgb(c):
    c = np.clip(c, 0, 1)
    return 255 * np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def make(unreal_dir: Path) -> Path:
    src = np.asarray(Image.open(unreal_dir / "textures" / f"{SOURCE}.png").convert("RGB"))
    src = src[SOURCE_ROWS[0]:SOURCE_ROWS[1]]
    img = synthesise(src)
    ring_tex = np.asarray(Image.open(unreal_dir / "textures" / "CLOUDS.png").convert("RGBA"))
    target = _linear(ring_tex[..., :3][ring_tex[..., 3] > 128]).mean(0)
    lin = _linear(img)
    img = _srgb(lin * (target / lin.reshape(-1, 3).mean(0)))
    dst = unreal_dir / "textures" / "CLOUD_FLOOR.png"
    Image.fromarray(img.astype(np.uint8)).save(dst)
    return dst
