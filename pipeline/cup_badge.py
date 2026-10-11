"""Custom cup badge (the course-select emblem picture of the custom Grand Prix).

    python -m pipeline.cup_badge picture.png

Built like the stock cup emblems: the Pumpkin Grand Prix shield from the game's emblem atlas
(UI_CourseSelect_CourseIcon_01, 2048^2 BC7, 4x4 cells of 512^2; extracted to extracted/cw/ui) with its pumpkin
painted out (each row of the inner panel blended from the panel's own edges), and the picture in its place: cut
out of a plain background (flood fill from the borders), edges cut by the picture's frame rounded off, white
outline and dark rim like the stock artwork. Written as a new texture: a copy of the stock 256^2 BC7 stage icon
renamed (same length) to UI_GPCustomCup13, with its mips replaced. The CustomGrandPrix UE4SS mod loads it by path
and puts it on the custom cup's emblem (UCourseSelectSubCategoryIcon::SetIconTexture). Output goes to
out/ui_cooked, which ue_build's package step adds to the mod.
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from pipeline.loading_screen import replace_mips
from pipeline.ue_build import ENGINE, ROOT

TEMPLATE = "01_Union/UI/System/Texture_StageIcon/UI_StageIcon1016"
BADGE = "01_Union/UI/System/Texture_StageIcon/UI_GPCustomCup13"
OUT = ROOT / "out" / "ui_cooked" / "UNION" / "Content"
ATLAS = ROOT / "extracted" / "cw" / "ui" / "UNION" / "Content" / "01_Union" / "UI" / "CourseSelect" / "Texture" / "UI_CourseSelect_CourseIcon_01.uexp"
CELL = (1, 1)            # (column, row) of the Pumpkin Grand Prix shield
PANEL = (117, 395, 104, 326)  # inner panel box painted over (x0, x1, y0, y1, cell pixels)
FIGURE = (256, 210, 280)      # picture centre x, y and size (cell pixels)


def atlas_cell() -> Image.Image:
    import texture2ddecoder
    u = ATLAS.read_bytes()
    pf = u.find(b"PF_BC7\0")
    w, h = struct.unpack_from("<ii", u, pf - 16)
    o = pf + 7 + 8 + 4  # format, (0, mip count), bulk-data index
    rgba = texture2ddecoder.decode_bc7(u[o:o + w * h], w, h)
    atlas = Image.frombytes("RGBA", (w, h), rgba, "raw", "BGRA")
    c, r = CELL
    return atlas.crop((c * 512, r * 512, c * 512 + 512, r * 512 + 512))


def empty_shield() -> Image.Image:
    cell = np.asarray(atlas_cell()).astype(np.float64)
    out = cell.copy()
    x0, x1, y0, y1 = PANEL
    s = (np.arange(x0, x1) - x0) / (x1 - x0)
    for y in range(y0, y1):
        left, right = cell[y, x0 - 5:x0].mean(0), cell[y, x1:x1 + 5].mean(0)
        out[y, x0:x1] = left * (1 - s)[:, None] + right * s[:, None]
    for y in range(y0 - 8, y0):  # feather into the art's surroundings
        k = (y - (y0 - 8)) / 8
        out[y, x0:x1] = cell[y, x0:x1] * (1 - k) + out[y0, x0:x1] * k
    for y in range(y1, y1 + 8):
        k = 1 - (y - y1) / 8
        out[y, x0:x1] = cell[y, x0:x1] * (1 - k) + out[y1 - 1, x0:x1] * k
    return Image.fromarray(out.clip(0, 255).astype(np.uint8), "RGBA")


def cut_out(picture: Image.Image) -> Image.Image:
    rgb = picture.convert("RGB")
    w, h = rgb.size
    padded = Image.new("RGB", (w + 2, h + 2), rgb.getpixel((0, 0)))
    padded.paste(rgb, (1, 1))
    ImageDraw.floodfill(padded, (0, 0), (255, 0, 255), thresh=40)
    a = np.asarray(padded)[1:-1, 1:-1]
    background = (a[..., 0] == 255) & (a[..., 1] == 0) & (a[..., 2] == 255)
    figure = picture.convert("RGBA")
    figure.putalpha(Image.fromarray(((~background) * 255).astype(np.uint8)).filter(ImageFilter.MedianFilter(3)))
    return figure


def draw(picture: Image.Image) -> Image.Image:
    shield = empty_shield()
    cx, cy, size = FIGURE
    figure = cut_out(picture).resize((size, size), Image.LANCZOS)
    oval = Image.new("L", (size, size), 0)
    ImageDraw.Draw(oval).ellipse((size * 0.02, size * 0.06, size * 0.98, size * 0.98), fill=255)
    alpha = np.asarray(figure.getchannel("A")).astype(np.float64) * np.asarray(oval.filter(ImageFilter.GaussianBlur(2))) / 255
    alpha = Image.fromarray(alpha.astype(np.uint8))
    figure.putalpha(alpha)
    white = alpha.point(lambda v: 255 if v > 100 else 0).filter(ImageFilter.MaxFilter(13)).filter(ImageFilter.GaussianBlur(1))
    dark = white.point(lambda v: 255 if v > 60 else 0).filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(1))
    layer = Image.new("RGBA", (size + 40, size + 40), (0, 0, 0, 0))
    for colour, mask in (((20, 24, 60, 255), dark), ((235, 238, 245, 255), white)):
        m = Image.new("L", layer.size, 0)
        m.paste(mask, (20, 20))
        layer.paste(Image.new("RGBA", layer.size, colour), (0, 0), m)
    layer.alpha_composite(figure, (20, 20))
    out = shield.copy()
    out.alpha_composite(layer, (cx - layer.size[0] // 2, cy - layer.size[1] // 2))
    pixels = np.asarray(out).copy()
    pixels[..., 3] = np.asarray(shield)[..., 3]  # nothing outside the shield
    return Image.fromarray(pixels, "RGBA")


CUSTOM_CELL = 10  # the atlas cell the emblem of cup 13 (Collabo_03) shows (WBP_CourseSelect_Sub_CourseIcon:
                  # GrandPrixId -> material Index: 13 -> 10, the unreleased "Crossover Grand Prix 3" shield)


def _mips(u: bytes, fmt: bytes):
    """(offset, width, height, size) of each mip's pixel data in a cooked texture export."""
    pf = u.find(fmt)
    w, h = struct.unpack_from("<ii", u, pf - 16)
    o = pf + len(fmt)
    count = struct.unpack_from("<i", u, o + 4)[0]
    o += 8
    bpp = {b"PF_BC7\0": 1.0, b"PF_G8\0": 1.0}[fmt]
    out = []
    for m in range(count):
        mw, mh = max(1, w >> m), max(1, h >> m)
        size = max(1, (mw + 3) // 4) * max(1, (mh + 3) // 4) * 16 if fmt == b"PF_BC7\0" else mw * mh
        o += 4
        assert struct.unpack_from("<ii", u, o + size)[:2] == (mw, mh), f"mip {m} layout"
        out.append((o, mw, mh, size))
        o += size + 12
    return out


def into_atlas(atlas_uexp: bytes, mask_uexp: bytes, badge: Image.Image, mask_cell: Image.Image) -> tuple[bytes, bytes]:
    """The emblem atlas (BC7) and its gem-glow mask (G8) with cell CUSTOM_CELL replaced in every mip; all other
    blocks are kept byte for byte (only the cell's 4x4 blocks are encoded)."""
    import texture2ddecoder
    from pipeline.loading_screen import encode_bc7
    out = bytearray(atlas_uexp)
    col, row = CUSTOM_CELL % 4, CUSTOM_CELL // 4
    for o, w, h, size in _mips(atlas_uexp, b"PF_BC7\0"):
        cw, ch = w // 4, h // 4
        if cw >= 4 and ch >= 4:  # block-aligned: encode just the cell, splice its blocks
            cell = np.asarray(badge.resize((cw, ch), Image.LANCZOS).convert("RGBA"))
            blocks = encode_bc7(cell)
            per_row = w // 4  # blocks per mip row
            for by in range(ch // 4):
                dst = o + ((row * ch // 4 + by) * per_row + col * cw // 4) * 16
                out[dst:dst + cw // 4 * 16] = blocks[by * cw // 4 * 16:(by + 1) * cw // 4 * 16]
        else:  # tiny mips: decode, paste, re-encode the whole mip
            bw, bh = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
            rgba = np.frombuffer(texture2ddecoder.decode_bc7(atlas_uexp[o:o + size], bw, bh), np.uint8).reshape(bh, bw, 4)[..., [2, 1, 0, 3]].copy()
            if cw >= 1 and ch >= 1:
                rgba[row * ch:(row + 1) * ch, col * cw:(col + 1) * cw] = np.asarray(badge.resize((cw, ch), Image.LANCZOS).convert("RGBA"))
            out[o:o + size] = encode_bc7(rgba[:h, :w] if (bw, bh) == (w, h) else rgba)[:size]
    mask = bytearray(mask_uexp)
    for o, w, h, size in _mips(mask_uexp, b"PF_G8\0"):
        cw, ch = w // 4, h // 4
        if cw < 1 or ch < 1:
            continue
        cell = np.asarray(mask_cell.resize((cw, ch), Image.LANCZOS).convert("L"))
        for y in range(ch):
            dst = o + (row * ch + y) * w + col * cw
            mask[dst:dst + cw] = cell[y].tobytes()
    return bytes(out), bytes(mask)


def renamed(data: bytes, required: bool = True) -> bytes:
    old, new = TEMPLATE.rsplit("/", 1)[1].encode(), BADGE.rsplit("/", 1)[1].encode()
    assert len(old) == len(new) and (old in data or not required)
    return data.replace(old, new)


def mask_cell(index: int) -> Image.Image:
    """A cell of the gem-glow mask (G8) of the atlas, at full size."""
    u = Path(str(ATLAS).replace(".uexp", "_Mask.uexp")).read_bytes()
    o, w, h, size = _mips(u, b"PF_G8\0")[0]
    full = Image.frombytes("L", (w, h), u[o:o + size])
    c, r = index % 4, index // 4
    return full.crop((c * w // 4, r * h // 4, (c + 1) * w // 4, (r + 1) * h // 4))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("picture", type=Path)
    args = ap.parse_args()
    badge = draw(Image.open(args.picture))
    preview = ROOT / "out" / "loading_screen" / "cup_badge.png"
    preview.parent.mkdir(parents=True, exist_ok=True)
    badge.save(preview)
    # into the stock emblem atlas, the custom cup's own cell (and the shield's gem glow into the mask)
    rel = "01_Union/UI/CourseSelect/Texture/UI_CourseSelect_CourseIcon_01"
    src_mask = Path(str(ATLAS).replace(".uexp", "_Mask.uexp"))
    atlas, mask = into_atlas(ATLAS.read_bytes(), src_mask.read_bytes(), badge, mask_cell(CELL[1] * 4 + CELL[0]))
    dst = OUT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    Path(str(dst) + ".uexp").write_bytes(atlas)
    Path(str(dst) + ".uasset").write_bytes(Path(str(ATLAS).replace(".uexp", ".uasset")).read_bytes())
    Path(str(dst) + "_Mask.uexp").write_bytes(mask)
    Path(str(dst) + "_Mask.uasset").write_bytes(src_mask.with_suffix(".uasset").read_bytes())
    old = OUT / BADGE  # the earlier separate texture is no longer used
    for ext in (".uasset", ".uexp"):
        Path(str(old) + ext).unlink(missing_ok=True)
    print(f"badge in cell {CUSTOM_CELL} of {rel} (+ mask); preview {preview}")


if __name__ == "__main__":
    main()
