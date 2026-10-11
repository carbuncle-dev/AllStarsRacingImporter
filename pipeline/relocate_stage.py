"""Move a staged stage mod off the stock stage it was built from, onto new stage ids (custom Grand Prix).

    relocate(staging, base="stg1016", stages=[3001, 3002, 3003])   (called by pipeline.package_mod --new-stages)

The mod is built over a template stage (E-Stadium, stg1016): its levels, collision, navigation, data asset,
minimap, intro sequence and menu pictures replace the stock ones. Here every staged package carrying the
template's id is renamed for the first new stage (DA_stg1016 -> DA_stg3001, other stg1016 -> asr3001,
UI_StageBG1016 -> UI_StageBG3001, ...:
same length, so cooked files are renamed byte for byte: file paths, and inside every staged package the name-map
entries naming a renamed package or its object). References to stock packages that are not shipped keep their
names, so the new stage still uses E-Stadium's untouched levels (sound, replay, NX...). The stock stage is then
no longer replaced at all.

The data asset becomes DA_stg3001 (StageId changed), copied to the other new ids (pointing at the same
content); DT_GrandPrixStages gets them as the custom cup's tracks (pipeline.custom_stage). Rows the game already
has for the unreleased debug stages 3001-3006 point at exactly these data asset names.
Template-only patches that only touch the stock stage (the stage-camera-settings tables) are dropped.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path

from pipeline.custom_stage import (DA_DIR, GP_TABLE, STAGE_ID_OFFSET, bgm_label, music_label, patched_gp_table,
                                   stage_index, with_bgm_label)
from pipeline.package_mod import ROOT
from pipeline.patch_object_levels import _name_map

DROP = ["01_Union/Scene/Common/StageCameraSettings/"]  # rows for the stock stage only


CONTENT_PREFIX = "asr"  # content of a new stage: /Game/.../asr3001/SL_asr3001... (see relocate)


def tokens(base: str, stage: int) -> list[tuple[str, str]]:
    """Renames, applied in order: the data asset keeps the stg<id> name the game's stage table points at;
    everything else gets a fresh name. (stg<id> paths of the unreleased debug stages are in the shipped
    AssetRegistry.bin -- their levels, collision, navigation -- and content shipped under those names
    loaded forever: the game took it for the never-shipped originals.)"""
    num = base[3:]
    pairs = [(f"DA_{base}", f"DA_stg{stage}"), (base, f"{CONTENT_PREFIX}{stage}")]
    pairs += [(f"Stage{kind}{num}", f"Stage{kind}{stage}") for kind in ("BG", "Tmb", "Icon", "Ring")]
    # the course intro's ambience cues (pipeline.ambience.own_sound_chain)
    pairs += [(f"{base.upper()}_COURSE_EXPLAIN", f"{CONTENT_PREFIX.upper()}{stage}_COURSE_EXPLAIN")]
    for old, new in pairs:
        assert len(old) == len(new)
    return pairs


def apply(s: str, pairs: list[tuple[str, str]]) -> str:
    for old, new in pairs:
        s = s.replace(old, new)
    return s


def fstring(s: str) -> bytes:
    """A name-map / FString entry as serialized (ANSI): i32 length with terminator, text, NUL."""
    return struct.pack("<i", len(s) + 1) + s.encode("latin-1") + b"\0"


def relocate(staging: Path, base: str, stages: list[int], music: str | None = None) -> dict[str, str]:
    """Returns the renamed packages (stock path -> new path), to register (pipeline.asset_registry)."""
    content = staging / "UNION" / "Content"
    pairs = tokens(base, stages[0])
    for prefix in DROP:
        for f in list((content / prefix).rglob("*")) if (content / prefix).exists() else []:
            if f.is_file():
                f.unlink()
        print(f"dropped {prefix} (stock-stage only)")
    # staged packages carrying the template id -> renamed
    files = [f for f in content.rglob("*") if f.is_file()]
    package_of = {f: "/Game/" + f.relative_to(content).with_suffix("").as_posix() for f in files}
    renamed = {p for p in package_of.values() if apply(p, pairs) != p}
    only = os.environ.get("RELOCATE_ONLY")  # experiments: rename only packages matching this regex
    if only:
        keep_da = {p for p in renamed if "/DataAsset/" in p}
        renamed = {p for p in renamed if re.search(only, p)} | keep_da
        print(f"RELOCATE_ONLY={only!r}: renaming {sorted(renamed)}")
    leaves = {p.rsplit("/", 1)[1] for p in renamed}

    def new_name(n: str) -> str | None:
        if n.startswith("/"):
            return apply(n, pairs) if n.split(".")[0] in renamed else None
        return apply(n, pairs) if n in leaves else None

    edits = 0
    for f in files:
        if f.suffix in (".uasset", ".umap"):
            data = f.read_bytes()
            for n in _name_map(data):
                new = new_name(n)
                if new and new != n:
                    assert data.count(fstring(n)) >= 1, (f, n)
                    data = data.replace(fstring(n), fstring(new))
                    edits += 1
            f.write_bytes(data)
        if package_of[f] in renamed:
            dst = content / apply(f.relative_to(content).as_posix(), pairs)
            dst.parent.mkdir(parents=True, exist_ok=True)
            f.rename(dst)
    print(f"renamed {len(renamed)} packages {pairs[1][0]} -> {pairs[1][1]} ({edits} name-map entries)")
    left = sorted({p for p in (("/Game/" + f.relative_to(content).with_suffix("").as_posix()) for f in content.rglob("*")
                                if f.is_file()) if base in p or base[3:] in p.rsplit("/", 1)[-1]})
    assert not left or only, f"still carrying the template id: {left[:5]}"
    # no shipped content may use a path the game's registry already knows (the debug stages' never-shipped
    # packages): the data assets are the only intended matches
    registry = ROOT / "extracted" / "cw" / "newtrack_research" / "registry" / "UNION" / "AssetRegistry.bin"
    if registry.exists():
        known = registry.read_bytes()
        clashes = [p for p in renamed if apply(p, pairs).encode() in known and "/DataAsset/" not in p]
        assert not clashes, f"renamed paths already in AssetRegistry.bin: {[apply(p, pairs) for p in clashes][:5]}"
    # the data asset: StageId, then one copy per extra stage id
    da = content / DA_DIR / f"DA_stg{stages[0]}"
    asset, export = da.with_suffix(".uasset").read_bytes(), bytearray(da.with_suffix(".uexp").read_bytes())
    assert export[STAGE_ID_OFFSET] == stage_index(int(base[3:])), "unexpected StageId byte"
    export[STAGE_ID_OFFSET] = stage_index(stages[0])
    if music:  # the track's own race-music label (pipeline.bgm_label)
        asset, new_export = with_bgm_label(asset, bytes(export), bgm_label(base), music_label(stages[0], music))
        export = bytearray(new_export)
        da.with_suffix(".uasset").write_bytes(asset)
        print(f"data asset music label: {music_label(stages[0], music)}")
    da.with_suffix(".uexp").write_bytes(export)
    for stage in stages[1:]:
        copy = content / DA_DIR / f"DA_stg{stage}"
        copy.with_suffix(".uasset").write_bytes(asset.replace(f"DA_stg{stages[0]}".encode(), f"DA_stg{stage}".encode()))
        export[STAGE_ID_OFFSET] = stage_index(stage)
        copy.with_suffix(".uexp").write_bytes(bytes(export))
    print(f"data assets: {', '.join(f'DA_stg{s}' for s in stages)} (content of stg{stages[0]})")
    if len(stages) == 3:
        table = content / GP_TABLE
        table.parent.mkdir(parents=True, exist_ok=True)
        t_asset, t_export = patched_gp_table(stages, base)
        table.with_suffix(".uasset").write_bytes(t_asset)
        table.with_suffix(".uexp").write_bytes(t_export)
        print(f"DT_GrandPrixStages: custom cup tracks {stages}")
    return {p: apply(p, pairs) for p in renamed if "/DataAsset/" not in p}
