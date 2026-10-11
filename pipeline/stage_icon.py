"""Stage icon (UI_StageIcon<id>, 256^2 BC7 with alpha: round badge on the HUD's "ROUND n" banner).

The badge frame is drawn like the stock icons (measured from E-Stadium's: dark inner band r 88-100 px,
purple ring to r 111, white rim to r 114, anti-aliased edge), with a crop of the loading-screen picture
inside (the SAMBA finish gate). Supersampled 4x. Written over a copy of the stock texture to out/ui_cooked.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.loading_screen import replace_mips
from pipeline.ue_build import ENGINE, ROOT

REL = "01_Union/UI/System/Texture_StageIcon/UI_StageIcon{id}"
OUT = ROOT / "out" / "ui_cooked" / "UNION" / "Content"
BANDS = [(88.0, (24, 28, 53)), (100.0, (99, 0, 255)), (111.0, (255, 255, 255)), (114.0, None)]  # band from this radius: colour
CROP = (0.5, 0.42, 0.30)  # centre x, y and half size (fractions of the picture): the gate and its suns


def draw(picture: Image.Image, size: int = 256, ss: int = 4) -> Image.Image:
    n = size * ss
    w, h = picture.size
    cx, cy, half = CROP
    inner = picture.crop((int((cx - half) * w), int((cy - half) * h), int((cx + half) * w), int((cy + half) * h)))
    inner = np.asarray(inner.convert("RGB").resize((n, n), Image.LANCZOS), np.float64)
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(xx - (n - 1) / 2, yy - (n - 1) / 2) / ss * (256 / size)
    rgb = inner.copy()
    edges = [b[0] for b in BANDS]           # 88, 100, 111, 114
    colours = [b[1] for b in BANDS[:-1]]    # dark, purple, white
    for r0, r1, colour in zip(edges[:-1], edges[1:], colours):
        rgb[(r >= r0) & (r < r1)] = colour
    alpha = (r < edges[-1]).astype(np.float64) * 255
    img = Image.fromarray(np.dstack([rgb, alpha]).astype(np.uint8), "RGBA")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("picture", type=Path)
    ap.add_argument("--stage", default="stg1016")
    args = ap.parse_args()
    rel = REL.format(id=args.stage[3:])
    src, dst = ENGINE / "UNION" / "Content" / rel, OUT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    icon = draw(Image.open(args.picture))
    icon.save(ROOT / "out" / "loading_screen" / "stage_icon.png")
    Path(str(dst) + ".uexp").write_bytes(replace_mips(Path(str(src) + ".uexp").read_bytes(), icon))
    shutil.copyfile(str(src) + ".uasset", str(dst) + ".uasset")
    print(f"wrote {dst}.uexp")


if __name__ == "__main__":
    main()
