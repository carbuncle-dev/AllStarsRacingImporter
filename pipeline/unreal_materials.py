"""Write materials.json for the Unreal build: material name -> {texture, masked}.

    python -m pipeline.unreal_materials out/samba_Hard/Samba_Hard.obj out/samba_Hard/unreal/materials.json

Covers every material used by the forests exported to Unreal (track only; envmap is reflection-only scenery).
"masked" marks textures with real transparency (alpha cut-outs); "kind" (opaque, masked, translucent,
water) comes from the texture's alpha and ASR's material data (<obj>.materials.json from forest_to_obj).
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
from PIL import Image

FORESTS = ("track.Forest", "item.Forest")  # item: objects placed by ASR's logic (pipeline.asr_objects)
WATER_FLAGS = 0x21001A3  # ASR pixel-shader flags of the water material (Water, Water3)
SOFT_ALPHA = 0.3         # share of partially transparent texels above which a material is translucent
ADDITIVE = ("waterfall", "lightfog")   # material name parts drawn additively
GLOWING = ("light_ani",)                # SAMBA_H_LIGHT bulbs
GLOW = 2.5                              # their emissive brightness
BEAM_PEAK = 0.2                         # brightest part of a coloured spotlight beam, x its colour
GOD_RAYS = 0.4                          # skeleton cave light shafts (lightfog): full strength was too bright, 0.15 too weak
# The last cave's coloured light shafts (light_lambert93) share the skeleton cave beams' texture; at the
# beams' boost they were far too strong there (ASR also darkens both by grey vertex colour, ~0.6)
LAST_CAVE_SHAFTS = {"light_lambert93": 0.5}  # 0.8 was still a bit strong
# Glow layers that are a copy of their material's picture (the billboard screen, the last cave's wooden
# walls): added at full strength they washed out (the walls made the last cave far too bright in game)
DIM_GLOW = {"billboard": 0.3, "mountwood": 0.3}
SPEAKER_WAVE_TINT = [0.85, 1.15, 1.45, 1.0]  # the loudspeakers' sound wave (SOUND_WAVE): light blue
SPEAKER_WAVE_GAIN = 2.5                 # its opacity boost (texture alpha <= 0.62, vertex alpha 0.42)


ALPHA_LAYER = 277  # ASR texture flags of a material's separate alpha (opacity) layer


GLOW_LAYER = (1157, 1173)  # ASR texture flags of a material's self-illumination layer


def asr_alpha_tested(mat: dict) -> bool:
    """Whether ASR cuts the material out by its texture's alpha: the record's unknown[5] is 2 for the alpha
    tested ones (fences, skeleton figures, flags; 4 for blended effects) and 0 for opaque ones."""
    return mat["unknown"][5] != 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("obj", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    folder = args.obj.parent
    tex, cur = {}, None
    for line in args.obj.with_suffix(".mtl").read_text(errors="replace").splitlines():
        if line.startswith("newmtl "):
            cur = line[7:].strip()
        elif line.startswith("map_Kd ") and cur and cur not in tex:
            tex[cur] = line[7:].strip()
    used, obj = set(), ""
    for line in open(args.obj, errors="replace"):
        if line.startswith("o "):
            obj = line[2:].strip()
        elif line.startswith("usemtl ") and obj.startswith(FORESTS):
            used.add(line[7:].strip())
    alpha: dict[Path, tuple[bool, float]] = {}
    sidecar = args.obj.with_suffix(".materials.json")
    asr = json.loads(sidecar.read_text()) if sidecar.exists() else {"materials": {}, "meshes": []}
    transforms = {}
    for mesh in asr["meshes"]:
        if mesh["name"].startswith(FORESTS) and mesh["uv_matrix"] != 255:
            transforms.setdefault(mesh["material"], mesh["transform"])

    def asr_texture(name: str) -> str | None:
        p = (folder / Path(name.replace("\\", "/")).name).resolve()
        return str(p) if p.exists() else None

    out = {}
    for m in sorted(used):
        p = (folder / tex[m]).resolve() if m in tex else None
        if p and not p.exists():
            p = None
        if p and p not in alpha:
            im = Image.open(p)
            a = np.asarray(im.getchannel("A")).astype(int) if "A" in im.mode else None
            alpha[p] = (a is not None and bool((a < 250).any()),
                        float(((a > 15) & (a < 240)).mean()) if a is not None else 0.0)
        masked, soft = alpha.get(p, (False, 0.0))
        info = {"texture": str(p) if p else None, "masked": masked, "kind": "masked" if masked else "opaque"}
        mat = asr["materials"].get(m)
        mask = next((t["name"] for t in mat["textures"] if t["flags"] == ALPHA_LAYER), None) if mat else None
        if p and mask and asr_texture(mask):
            # A separate alpha layer (the balloon's burst skin: the colour texture torn by BALLOON_BURST_ALPHA,
            # which ASR scrolls across it): one cut-out texture, the layer's alpha as the colour's alpha
            # (the scroll is not reproduced).
            combined = folder / f"{p.stem}__{Path(mask).stem}.png"
            rgb = Image.open(p).convert("RGB")
            a_layer = Image.open(asr_texture(mask)).convert("RGBA").getchannel("A").resize(rgb.size)
            Image.merge("RGBA", (*rgb.split(), a_layer)).save(combined)
            info.update(texture=str(combined.resolve()), masked=True, kind="masked")
        if mat and mat["psflags"] == WATER_FLAGS:
            # ASR water: no colour texture -- vertex colour and alpha, a ripple normal map (the
            # texture the OBJ references, pink) bending a sparkle/highlight map (flags 128).
            env = next((asr_texture(t["name"]) for t in mat["textures"] if t["flags"] == 128), None)
            # floats[3:7] = (0.157, 1, 1, 1): the water's RGBA tint (cyan), applied to the vertex colour.
            tint = (mat["floats"][3:7] + [1.0] * 4)[:4] if len(mat["floats"]) > 3 else [1.0] * 4
            info.update(kind="water", ripple=info["texture"], env=env, tint=tint,
                        tiling=transforms.get(m, [0, 0, 0, 1, 1])[3:5])
        elif masked and soft > SOFT_ALPHA:
            # Soft-edged effects (waterfalls, light fog, cloud fades) drawn as hard cut-outs looked
            # like white blotches: alpha blend them (with the vertex alpha ASR fades their edges by).
            # Waterfalls and light shafts brighten what is behind them in ASR's footage: additive.
            # Everything scrolls at ASR's per-piece rates (Pan/Flip UVs from export_for_unreal).
            kind = "additive" if any(k in m.lower() for k in ADDITIVE) else "translucent"
            if "waterfall" in m.lower():
                kind = "waterfall"  # alpha-blended veil (M_ASR_Waterfall), not additive
                if mat and any(t["flags"] == 405 for t in mat["textures"]):  # normal-mapped stream
                    info["tint"] = [0.55, 0.9, 1.0, 1.0]
            info.update(kind=kind, pan=0.0)
            if kind in ("additive", "waterfall") and mat:
                # Waterfalls are layered: slot 1 a second streak texture (flags 1173), slot 2 a sparkle
                # map ("_S", black with white specks); each slot scrolls with its own UV bone (Pan2/Pan3).
                names = [t["name"] for t in mat["textures"]]
                layer2 = next((t["name"] for t in mat["textures"] if t["flags"] == 1173), None)
                sparkle = next((n for n in names if os.path.splitext(n)[0].upper().endswith("_S")), None)
                if layer2 and asr_texture(layer2):
                    info["layer2"] = asr_texture(layer2)
                if sparkle and asr_texture(sparkle):
                    info["sparkle"] = asr_texture(sparkle)
        if "lightfog" in m.lower() and info["kind"] == "additive":
            info["intensity"] = GOD_RAYS
        if "soundwave" in m.lower():
            # ASR's speaker wave: a light, see-through blue. Its grey-blue stripes at their own faint opacity
            # tinted the yellow scenery green (alpha blended) or yellow (additive): drawn like the waterfall
            # veils, tinted light blue with a raised opacity
            info.update(kind="waterfall", masked=False, tint=SPEAKER_WAVE_TINT, alpha_gain=SPEAKER_WAVE_GAIN)
        if any(k in m.lower() for k in GLOWING):
            info["emissive_scale"] = GLOW  # ASR's coloured light bulbs shine (and switch colour: flipbook)
        if mat and info["kind"] == "masked" and mat["unknown"][5] == 4 and p:
            # ASR blends these (unknown[5] = 4): the cave's coloured spotlight beams, whose alpha stays
            # under 0.11 -- as cut-outs they vanished entirely. Additive, colour x alpha, scaled so the
            # strongest part of a beam reaches a quarter of its colour (half was too strong in game, a quarter still a bit).
            peak = float(np.asarray(Image.open(p).convert("RGBA").getchannel("A")).max()) / 255
            if 0 < peak < 0.5:
                info.update(kind="additive", masked=False, intensity=LAST_CAVE_SHAFTS.get(m, round(BEAM_PEAK / peak, 2)))
        if mat and info["kind"] == "masked" and info["masked"] and not asr_alpha_tested(mat):
            # ASR draws this material opaque: its texture's alpha is unused (the signboards' white
            # backgrounds came out see-through as cut-outs)
            info.update(kind="opaque", masked=False)
        if mat and info["kind"] in ("opaque", "masked"):
            # ASR's self-illumination layer ("_L" / "_LE": the cave's glowing bones and track, the
            # billboard's screen frames), added to the colour by the masters' Glow parameter
            glow = next(((i, t["name"]) for i, t in enumerate(mat["textures"]) if t["flags"] in GLOW_LAYER), None)
            if glow and glow[0] in (1, 2) and asr_texture(glow[1]):
                info.update(glow=asr_texture(glow[1]), glow_slot=glow[0])
                for part, scale in DIM_GLOW.items():
                    if part in m.lower():
                        info["glow_scale"] = scale
        out[m] = info
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=1))
    kinds = __import__("collections").Counter(v["kind"] for v in out.values())
    print(f"{len(out)} materials, {sum(1 for v in out.values() if v['texture'])} textured: {dict(kinds)}")


if __name__ == "__main__":
    main()
