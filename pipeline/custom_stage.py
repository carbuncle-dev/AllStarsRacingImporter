"""Stage data assets for new stage ids (custom Grand Prix), packed into their own IoStore mod.

    python -m pipeline.custom_stage --base stg1016 --stage 3001

The game's cooked stage table (CDT_StageDataAsset) already has rows for the unreleased debug stages
3001-3006 / 4001-4007, pointing at PrimaryStageDataAsset:DA_stg3001 ... -- data assets that were registered
in the shipped AssetRegistry.bin but never packaged. A copy of the base stage's data asset renamed to one of
those names (same-length byte rename: DA_stg1016 -> DA_stg3001) with its StageId changed fills that hole
without touching the stage table or the registry.

The copies still load the base stage's levels (only the asset's own name and StageId change). With three
stages, the custom cup (hidden placeholder cup 13, Collabo_03) gets them as its tracks in DT_GrandPrixStages.
Output: out/mods/zzz_CustomGP_P.{utoc,ucas,pak}, round-tripped through retoc to check the result.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from pipeline.package_mod import PAKS, RETOC, ROOT
from pipeline.patch_object_levels import _name_map

SOURCE = ROOT / "extracted" / "cw" / "newtrack_research" / "UNION" / "Content"
DA_DIR = "01_Union/Database/Stage/DataAsset"
STAGE_ID_OFFSET = 10  # export offset of the StageId byte (first property, after the unversioned header)


def stage_index(stage: int) -> int:
    """EStageId value of stage number `stage` (UnionSystem/Public/EStageId.h order)."""
    ranges = [(1001, 1042), (1501, 1510), (1901, 1902), (2001, 2020), (3001, 3010), (4001, 4010)]
    index = 0
    for lo, hi in ranges:
        if lo <= stage <= hi:
            return index + stage - lo
        index += hi - lo + 1
    raise ValueError(f"stage {stage} is not an EStageId")


def bgm_label(base: str) -> str:
    """The stage's race-music selector label (UPrimaryStageDataAsset::BGMName, e.g. "stg1016_EStadium")."""
    import re
    export = (SOURCE / DA_DIR / f"DA_{base}").with_suffix(".uexp").read_bytes()
    m = re.search(rb"\x00(" + base.encode() + rb"_[A-Za-z0-9]+)\x00", export)
    assert m, f"no BGMName in DA_{base}"
    return m.group(1).decode()


def music_label(stage: int, name: str) -> str:
    """The music label a custom stage gets (pipeline.bgm_label adds it to the game's sound settings)."""
    return f"stg{stage}_{name}"


def with_bgm_label(asset: bytes, export: bytes, old: str, new: str) -> tuple[bytes, bytes]:
    """The data asset with its BGMName FString replaced (size fields follow)."""
    o = struct.pack("<i", len(old) + 1) + old.encode() + b"\0"
    n = struct.pack("<i", len(new) + 1) + new.encode() + b"\0"
    assert export.count(o) == 1, f"BGMName {old!r} not found once"
    new_export = export.replace(o, n)
    return resized(asset, len(export), len(new_export)), new_export


def renamed_data_asset(base: str, stage: int) -> tuple[bytes, bytes]:
    src = SOURCE / DA_DIR / f"DA_{base}"
    asset, export = src.with_suffix(".uasset").read_bytes(), bytearray(src.with_suffix(".uexp").read_bytes())
    old, new = f"DA_{base}".encode(), f"DA_stg{stage}".encode()
    assert len(old) == len(new), "same-length rename only"
    asset = asset.replace(old, new)
    base_index, new_index = stage_index(int(base[3:])), stage_index(stage)
    assert export[STAGE_ID_OFFSET] == base_index, f"StageId byte is {export[STAGE_ID_OFFSET]}, expected {base_index}"
    export[STAGE_ID_OFFSET] = new_index
    names = _name_map(asset)
    assert f"DA_stg{stage}" in names and f"/Game/{DA_DIR}/DA_stg{stage}" in names
    return asset, bytes(export)


GP_TABLE = "01_Union/Database/GrandPrix/DT_GrandPrixStages"
CUSTOM_CUP = "EGrandPrixId::Collabo_03"  # hidden placeholder cup 13 (the CustomGrandPrix UE4SS mod shows it)
# Stage-id names used only by the hidden placeholder cups (SEGA GP 2/3: unreleased stages 1010-1012), renamed
# (same length) to the new stages, so the table needs no new names.
SPARE_NAMES = ["EStageId::STG1010", "EStageId::STG1011", "EStageId::STG1012"]


def name_refs(export: bytes, names: list[str]) -> list[tuple[int, str]]:
    """(offset, name) of every FName reference (index, number 0) to an enum value name in the export."""
    refs, o = [], 0
    while o + 8 <= len(export):
        i, number = struct.unpack_from("<ii", export, o)
        if 0 <= i < len(names) and number == 0 and "::" in names[i]:
            refs.append((o, names[i]))
            o += 8
        else:
            o += 1
    return refs


def stage_settings(export: bytes, at: int) -> tuple[list[tuple[int, bytes]], int]:
    """The cup row's Stages array (TArray<FGrandPrixStageSetting>, unversioned) at `at`: [(MainStageId, element
    bytes)] and the end offset. Element: uint16 header 0x0500 (2 values), MainStageId byte, then
    DefaultTravelStageSettings: int32 count of {uint16 header (bit 7: zero mask follows; bits 9-15: value count),
    zero-mask byte, the non-zero values (TravelStageId byte, bOpen, bIrregular)}."""
    count = struct.unpack_from("<i", export, at)[0]
    assert 0 < count <= 32, count
    o, out = at + 4, []

    def header(o: int) -> tuple[list[bool], int]:
        """present flags of the struct's values and the offset after the header (one fragment, bIsLast)."""
        h = struct.unpack_from("<H", export, o)[0]
        assert h & 0x100 and not h & 0x7F, f"unexpected unversioned header {h:#x}"
        values, o = h >> 9, o + 2
        zero = 0
        if h & 0x80:
            zero, o = export[o], o + 1
        return [not zero >> i & 1 for i in range(values)], o

    for _ in range(count):
        start = o
        present, o = header(o)
        main = 0
        if present[0]:
            main, o = export[o], o + 1
        if len(present) > 1 and present[1]:
            travel, o = struct.unpack_from("<i", export, o)[0], o + 4
            for _ in range(travel):
                flags, o = header(o)
                o += sum(flags)  # TravelStageId byte, bOpen, bIrregular (one byte each when non-zero)
        out.append((main, export[start:o]))
    return out, o


def patched_gp_table(stages: list[int], base: str = "stg1016") -> tuple[bytes, bytes]:
    """DT_GrandPrixStages with the custom cup's three tracks set to `stages`. Each cup row lists its tracks as
    three EStageId names after its EGrandPrixId name (StageId: what the race loads), then the same tracks again
    as FGrandPrixStageSetting (Stages: the course-select cards and each track's travel-ring destinations). The
    custom cup gets the base stage's setting (its travel rings) for each new stage."""
    assert len(stages) == 3
    src = SOURCE / GP_TABLE
    asset, export = src.with_suffix(".uasset").read_bytes(), bytearray(src.with_suffix(".uexp").read_bytes())
    names = _name_map(asset)
    refs = name_refs(bytes(export), names)
    cup = next(k for k, (_, n) in enumerate(refs) if n == CUSTOM_CUP)
    tracks = [o for o, n in refs[cup + 1:cup + 4] if n.startswith("EStageId::")]
    assert len(tracks) == 3, refs[cup:cup + 4]
    for spare, stage, offset in zip(SPARE_NAMES, stages, tracks):
        new = f"EStageId::STG{stage}"
        assert len(new) == len(spare) and asset.count(spare.encode() + b"\0") == 1
        asset = asset.replace(spare.encode() + b"\0", new.encode() + b"\0")
        struct.pack_into("<i", export, offset, names.index(spare))
    renamed = _name_map(asset)
    got = [renamed[struct.unpack_from("<i", export, o)[0]] for o in tracks]
    assert got == [f"EStageId::STG{s}" for s in stages], got
    # the base stage's FGrandPrixStageSetting, from the cup that has it
    base_index, template = stage_index(int(base[3:])), None
    for k, (o, n) in enumerate(refs):
        if n.startswith("EGrandPrixId::") and n != CUSTOM_CUP:
            ids = []
            for p, m in refs[k + 1:]:
                if not m.startswith("EStageId::"):
                    break
                ids.append(p)
            if ids:
                for main, element in stage_settings(bytes(export), ids[-1] + 8)[0]:
                    if main == base_index:
                        template = element
    assert template is not None, f"no cup has {base}"
    old, end = stage_settings(bytes(export), tracks[-1] + 8)
    assert template[:2] == b"\x00\x05", "base stage setting: expected both values present"
    new = struct.pack("<i", 3) + b"".join(template[:2] + bytes([stage_index(s)]) + template[3:] for s in stages)
    print(f"DT_GrandPrixStages: custom cup settings {[m for m, _ in old]} -> {[stage_index(s) for s in stages]} "
          f"(travel rings of {base})")
    export = bytes(export[:tracks[-1] + 8]) + new + bytes(export[end:])
    return resized(asset, len(src.with_suffix(".uexp").read_bytes()), len(export)), export


def resized(asset: bytes, old_uexp: int, new_uexp: int) -> bytes:
    """The package summary with the single export's SerialSize and BulkDataStartOffset following the .uexp size
    (both int64; the .uexp ends with the 4-byte package tag)."""
    asset = bytearray(asset)
    for old, new in ((old_uexp - 4, new_uexp - 4), (len(asset) + old_uexp - 4, len(asset) + new_uexp - 4)):
        pat = struct.pack("<q", old)
        hits = [i for i in range(len(asset) - 7) if asset[i:i + 8] == pat]
        assert len(hits) == 1, f"size field {old} found {len(hits)} times"
        struct.pack_into("<q", asset, hits[0], new)
    return bytes(asset)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="stg1016")
    ap.add_argument("--stage", type=int, action="append", help="new stage number(s), e.g. 3001")
    ap.add_argument("--name", default="CustomGP")
    args = ap.parse_args()
    stages = args.stage or [3001, 3002, 3003]
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "legacy" / "UNION" / "Content" / DA_DIR
        staging.mkdir(parents=True)
        if len(stages) == 3:
            table = Path(tmp) / "legacy" / "UNION" / "Content" / GP_TABLE
            table.parent.mkdir(parents=True)
            asset, export = patched_gp_table(stages)
            table.with_suffix(".uasset").write_bytes(asset)
            table.with_suffix(".uexp").write_bytes(export)
            print(f"DT_GrandPrixStages: {CUSTOM_CUP} tracks -> {stages}")
        for stage in stages:
            asset, export = renamed_data_asset(args.base, stage)
            (staging / f"DA_stg{stage}.uasset").write_bytes(asset)
            (staging / f"DA_stg{stage}.uexp").write_bytes(export)
            print(f"DA_stg{stage}: copy of DA_{args.base}, StageId {stage_index(stage)}")
        out = ROOT / "out" / "mods"
        out.mkdir(parents=True, exist_ok=True)
        utoc = out / f"zzz_{args.name}_P.utoc"
        subprocess.run([str(RETOC), "to-zen", "--version", "UE5_4", str(Path(tmp) / "legacy"), str(utoc)], check=True,
                       capture_output=True)
        # round trip: the container decodes back to the same bytes
        zen, check = Path(tmp) / "zen", Path(tmp) / "check"
        zen.mkdir()
        for f in [PAKS / "global.utoc", PAKS / "global.ucas"] + [utoc.with_suffix(e) for e in (".utoc", ".ucas", ".pak")]:
            shutil.copy(f, zen / f.name)
        subprocess.run([str(RETOC), "to-legacy", "--version", "UE5_4", "--no-shaders", str(zen), str(check)], check=True,
                       capture_output=True)
        for stage in stages:
            got = next(check.rglob(f"DA_stg{stage}.uexp")).read_bytes()
            assert got[STAGE_ID_OFFSET] == stage_index(stage), "StageId lost in the round trip"
            assert f"DA_stg{stage}" in _name_map(next(check.rglob(f"DA_stg{stage}.uasset")).read_bytes())
        if len(stages) == 3:
            got = next(check.rglob("DT_GrandPrixStages.uasset"))
            names = _name_map(got.read_bytes())
            refs = name_refs(got.with_suffix(".uexp").read_bytes(), names)
            cup = next(k for k, (_, n) in enumerate(refs) if n == CUSTOM_CUP)
            assert [n for _, n in refs[cup + 1:cup + 4]] == [f"EStageId::STG{s}" for s in stages]
        print("IoStore round trip OK")
    for ext in (".utoc", ".ucas", ".pak"):
        f = utoc.with_suffix(ext)
        print(f"  {f}  ({f.stat().st_size if f.exists() else 'missing'} bytes)")


if __name__ == "__main__":
    main()
