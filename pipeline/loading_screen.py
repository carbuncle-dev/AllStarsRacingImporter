"""Replace the replaced course's pictures: the race-intro/loading background and the course-select thumbnail.

    python -m pipeline.loading_screen <picture.png> --stage stg1016

UI_StageBG<id> (2048^2, 1 mip; WBP_RaceBefore_*; shown stretched to 16:9, so stored squeezed: --wide) and UI_StageTmb<id> (1024^2, 11 mips; course select)
are cooked BC7 textures whose mips sit inline in the .uexp after the pixel-format name: i32 mip count,
then per mip i32 bulk-data resource index, the BC7 blocks (size fixed by the dimensions), i32 x, y, z.
The picture is resized to each texture, mipmapped, BC7-encoded with the engine's Intel ISPC compressor
and written over the original blocks -- same sizes, so no header changes. Output goes to out/ui_cooked
(UNION/Content/...), which ue_build's package step adds to the mod.

The originals come from the editor project's copies of the game's cooked UI (legacy .uasset/.uexp).

Editor renders come out dark and clipped-saturated (median HSV saturation 1.0, luma 0.31), and the
scene capture ignores exposure settings, so the picture is graded here: in linear light, saturation
scaled around luminance, exposure gain, soft highlight shoulder -- solved for E-Stadium's own picture
(UI_StageBG1016: mean saturation 0.55, mean luma 0.44). --no-grade keeps the picture as is.
"""

from __future__ import annotations

import argparse
import ctypes
import shutil
import struct
from pathlib import Path

import numpy as np
from PIL import Image

from pipeline.ue_build import ENGINE, PROJECT, ROOT

GAME_CONTENT = PROJECT.parent / "Content"
TEXTURES = ("01_Union/UI/System/Texture_StageBG/UI_StageBG{id}", "01_Union/UI/System/Texture_StageTmb/UI_StageTmb{id}")
OUT = ROOT / "out" / "ui_cooked"
ISPC = ENGINE / "Engine" / "Binaries" / "ThirdParty" / "Intel" / "ISPCTexComp" / "Win64-Release" / "ispc_texcomp.dll"


class _Surface(ctypes.Structure):
    _fields_ = [("ptr", ctypes.c_void_p), ("width", ctypes.c_int32), ("height", ctypes.c_int32),
                ("stride", ctypes.c_int32)]


class _BC7Settings(ctypes.Structure):
    _fields_ = [("mode_selection", ctypes.c_bool * 4), ("refineIterations", ctypes.c_int * 8),
                ("skip_mode2", ctypes.c_bool), ("fastSkipTreshold_mode1", ctypes.c_int),
                ("fastSkipTreshold_mode3", ctypes.c_int), ("fastSkipTreshold_mode7", ctypes.c_int),
                ("mode45_channel0", ctypes.c_int), ("refineIterations_channel", ctypes.c_int),
                ("channels", ctypes.c_int)]


def encode_bc7(rgba: np.ndarray) -> bytes:
    """RGBA8 (h, w, 4) -> BC7 blocks; mips smaller than a block are padded by edge replication."""
    h, w = rgba.shape[:2]
    bw, bh = max(4, (w + 3) // 4 * 4), max(4, (h + 3) // 4 * 4)
    if (bw, bh) != (w, h):
        rgba = np.pad(rgba, ((0, bh - h), (0, bw - w), (0, 0)), mode="edge")
    src = np.ascontiguousarray(rgba, dtype=np.uint8)
    lib = ctypes.CDLL(str(ISPC))
    settings = _BC7Settings()
    # the opaque profile drops alpha (the course map came out on black): alpha profile when it matters
    if (src[..., 3] < 255).any():
        lib.GetProfile_alpha_slow(ctypes.byref(settings))
    else:
        lib.GetProfile_slow(ctypes.byref(settings))
    surface = _Surface(src.ctypes.data, bw, bh, bw * 4)
    dst = ctypes.create_string_buffer(bw * bh)  # 16 bytes per 4x4 block
    lib.CompressBlocksBC7(ctypes.byref(surface), dst, ctypes.byref(settings))
    return dst.raw


TARGET_SATURATION, TARGET_LUMA = 0.55, 0.45


def _grade(img: Image.Image, gain: float, sat: float) -> np.ndarray:
    src = np.asarray(img, np.float64) / 255
    lin = np.where(src <= 0.04045, src / 12.92, ((src + 0.055) / 1.055) ** 2.4)
    y = lin @ np.array([0.2126, 0.7152, 0.0722])
    c = np.clip(y[..., None] + (lin - y[..., None]) * sat, 0, None) * gain
    c = np.clip(c / (1 + c / 4) * 1.25, 0, 1)  # shoulder, white stays white
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


def grade(img: Image.Image) -> Image.Image:
    small = img.resize((256, 256))

    def error(g, s):
        o = _grade(small, g, s)
        mx, mn = o.max(-1), o.min(-1)
        saturation = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0).mean()
        luma = (o @ np.array([0.299, 0.587, 0.114])).mean()
        return (saturation - TARGET_SATURATION) ** 2 + (luma - TARGET_LUMA) ** 2

    _, g, s = min((error(g, s), g, s) for g in np.linspace(1, 4, 31) for s in np.linspace(0.3, 1, 29))
    print(f"grade: exposure x{g:.2f}, saturation x{s:.2f}")
    return Image.fromarray((_grade(img, g, s) * 255 + 0.5).astype(np.uint8))


def replace_mips(uexp: bytes, picture: Image.Image) -> bytes:
    pf = uexp.find(b"PF_BC7\0")
    if pf < 0:
        raise SystemExit("not a BC7 texture")
    w, h = struct.unpack_from("<ii", uexp, pf - 16)
    o = pf + 7
    if struct.unpack_from("<i", uexp, o)[0] != 0:
        raise SystemExit("unexpected texture header")
    mips = struct.unpack_from("<i", uexp, o + 4)[0]
    o += 8
    out = bytearray(uexp)
    for m in range(mips):
        mw, mh = max(1, w >> m), max(1, h >> m)
        size = max(1, (mw + 3) // 4) * max(1, (mh + 3) // 4) * 16
        o += 4  # bulk-data resource index
        if struct.unpack_from("<ii", uexp, o + size)[:2] != (mw, mh):
            raise SystemExit(f"mip {m}: layout mismatch at {o + size}")
        img = picture.resize((mw, mh), Image.LANCZOS) if (mw, mh) != picture.size else picture
        out[o:o + size] = encode_bc7(np.asarray(img.convert("RGBA")))
        o += size + 12
    return bytes(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("picture", type=Path)
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--no-grade", action="store_true")
    ap.add_argument("--wide", type=Path, help="16:9 picture for the loading screen (UI_StageBG), which the "
                    "game stretches from the square texture to the screen: stored squeezed")
    args = ap.parse_args()
    picture = Image.open(args.picture).convert("RGB")
    side = min(picture.size)  # the textures are square: centre crop
    left, top = (picture.width - side) // 2, (picture.height - side) // 2
    picture = picture.crop((left, top, left + side, top + side))
    if not args.no_grade:
        picture = grade(picture)
    wide = Image.open(args.wide).convert("RGB") if args.wide else picture
    if args.wide and not args.no_grade:
        wide = grade(wide)
    for rel in TEXTURES:
        image = wide if "StageBG" in rel else picture  # replace_mips resizes (squeezes) to the texture
        rel = rel.format(id=args.stage[3:])
        src, dst = GAME_CONTENT / rel, OUT / "UNION" / "Content" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src) + ".uasset", str(dst) + ".uasset")
        Path(str(dst) + ".uexp").write_bytes(replace_mips(Path(str(src) + ".uexp").read_bytes(), image))
        print(f"wrote {dst}.uexp")


if __name__ == "__main__":
    main()
