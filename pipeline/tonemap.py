"""Unreal's default filmic tonemapper in numpy, and a Radiance .hdr reader for editor scene captures.

Used to turn a linear HDR capture (tools/ue_capture.py, SCS_SCENE_COLOR_HDR) into what the game shows:
FilmToneMap from Engine/Shaders/Private/TonemapCommon.ush with UE5's default film settings (slope 0.88,
toe 0.55, shoulder 0.26, black clip 0, white clip 0.04), in the ACES AP1 working space with its pre-
(0.96) and post- (0.93) desaturation; output is display sRGB.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

SRGB_TO_AP1 = np.array([[0.6131178129, 0.3411819959, 0.0457873443],
                        [0.0699340823, 0.9181030375, 0.0119327755],
                        [0.0204629926, 0.1067686634, 0.8727159106]])
AP1_TO_SRGB = np.linalg.inv(SRGB_TO_AP1)
AP1_Y = np.array([0.2722287168, 0.6740817658, 0.0536895174])


def film_tonemap(linear: np.ndarray, slope=0.88, toe=0.55, shoulder=0.26, black_clip=0.0,
                 white_clip=0.04) -> np.ndarray:
    """Linear sRGB-primaries scene colour (..., 3) -> display sRGB in [0, 1]."""
    c = np.maximum(linear @ SRGB_TO_AP1.T, 1e-10)
    c = (c @ AP1_Y)[..., None] * (1 - 0.96) + c * 0.96
    toe_scale = 1 + black_clip - toe
    shoulder_scale = 1 + white_clip - shoulder
    bt = (0.18 + black_clip) / toe_scale - 1
    toe_match = np.log10(0.18) - 0.5 * np.log((1 + bt) / (1 - bt)) * (toe_scale / slope)
    straight_match = (1 - toe) / slope - toe_match
    shoulder_match = shoulder / slope - straight_match
    log_c = np.log10(np.maximum(c, 1e-10))
    straight = slope * (log_c + straight_match)
    toe_c = -black_clip + (2 * toe_scale) / (1 + np.exp((-2 * slope / toe_scale) * (log_c - toe_match)))
    shoulder_c = (1 + white_clip) - (2 * shoulder_scale) / (
        1 + np.exp((2 * slope / shoulder_scale) * (log_c - shoulder_match)))
    toe_c = np.where(log_c < toe_match, toe_c, straight)
    shoulder_c = np.where(log_c > shoulder_match, shoulder_c, straight)
    t = np.clip((log_c - toe_match) / (shoulder_match - toe_match), 0, 1)
    if shoulder_match < toe_match:  # as in UE: the default settings overlap toe and shoulder
        t = 1 - t
    t = (3 - 2 * t) * t * t
    tone = toe_c + (shoulder_c - toe_c) * t
    tone = (tone @ AP1_Y)[..., None] * (1 - 0.93) + tone * 0.93
    out = np.clip(np.maximum(tone, 0) @ AP1_TO_SRGB.T, 0, 1)
    return np.where(out <= 0.0031308, out * 12.92, 1.055 * out ** (1 / 2.4) - 0.055)


def read_hdr(path: Path) -> np.ndarray:
    """Radiance RGBE (.hdr, new-style RLE or flat) -> float32 (h, w, 3)."""
    d = Path(path).read_bytes()
    end = d.index(b"\n\n") + 2
    m = re.match(rb"([-+])Y (\d+) ([-+])X (\d+)\n", d[end:])
    h, w = int(m.group(2)), int(m.group(4))
    o = end + m.end()
    data = np.frombuffer(d, np.uint8, offset=o)
    img = np.empty((h, w, 4), np.uint8)
    p = 0
    for y in range(h):
        if w >= 8 and data[p] == 2 and data[p + 1] == 2 and (data[p + 2] << 8 | data[p + 3]) == w:
            p += 4
            for ch in range(4):
                x = 0
                while x < w:
                    n = int(data[p]); p += 1
                    if n > 128:
                        n -= 128
                        img[y, x:x + n, ch] = data[p]; p += 1
                    else:
                        img[y, x:x + n, ch] = data[p:p + n]; p += n
                    x += n
        else:
            img[y] = data[p:p + 4 * w].reshape(w, 4); p += 4 * w
    e = img[..., 3].astype(np.int32)
    scale = np.where(e > 0, np.ldexp(1.0, e - 136), 0.0)
    rgb = img[..., :3] * scale[..., None]
    if m.group(1) == b"+":  # +Y: bottom-up
        rgb = rgb[::-1]
    return rgb.astype(np.float32)
