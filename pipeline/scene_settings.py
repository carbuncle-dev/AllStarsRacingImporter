"""Cutscene camera settings for the replaced stage: day-course values instead of E-Stadium's night ones.

    python -m pipeline.scene_settings --stage stg1016 --like stg1022

Race lighting matches Metal Harbor (stg1022, pipeline.ue_build), but the game's cutscenes set their own
exposure and bloom:

* Scene/Common/StageCameraSettings/DT_StageCameraSettings_* (rival intro, racer intro, overview,
  ceremony, festa / rival result): one FStageCameraSettings row per stage (cw.stage_camera). The
  stage's row gets the reference stage's values, patched in place (same stored fields, same size).
* Scene/IntroduceCourse/LS_<stage> (stage intro fly-over): its cine cameras override exposure and
  bloom (UE4SS dump 2026-10-09: manual bias 10.2 at f/2.8; bloom 2 / 3 / 6 at threshold -1, i.e. every
  pixel glows). Patched in place: the unique 10.2 -> the race exposure at f/2.8
  (log2(2.8^2 * 60) - 0.907 = 7.97, as Metal Harbor's own cutscene rows use ~8), bloom intensities -> 0.5.

Sources are the game's 1.4.3 assets (retoc to-legacy into extracted/cw/scene143); output goes to
out/ui_cooked, which ue_build's package step adds to the mod.
"""

from __future__ import annotations

import argparse
import math
import struct
from pathlib import Path

from cw.stage_camera import read
from pipeline.ue_build import ROOT

SRC = ROOT / "extracted" / "cw" / "scene143" / "UNION" / "Content"
OUT = ROOT / "out" / "ui_cooked" / "UNION" / "Content"
TABLES = "01_Union/Scene/Common/StageCameraSettings"
FIELDS = ("BloomIntensity", "BloomThreshold", "AutoExposureBias", "CameraShutterSpeed", "CurrentAperture",
          "AutoExposureMethod")
RACE_EXPOSURE_BIAS_F28 = round(math.log2(2.8 ** 2 * 60.0) - 0.907, 2)
# LS_stg1016: offsets of the cine cameras' values (verified unique / adjacent to the dumped values)
INTRO_PATCHES = {389: (10.2, RACE_EXPOSURE_BIAS_F28),      # CineCameraActor_14 AutoExposureBias (manual)
                 381: (2.0, 0.5), 515: (3.0, 0.5), 623: (6.0, 0.5)}  # BloomIntensity of the three cameras


def stage_index(stage: str) -> int:
    return int(stage[3:]) - 1001  # EStageId: STG1001 = 0 ... (contiguous in the 10xx range)


def patch_tables(stage: str, like: str) -> None:
    sid, ref = stage_index(stage), stage_index(like)
    for uexp in sorted((SRC / TABLES).glob("DT_StageCameraSettings_*.uexp")):
        data = bytearray(uexp.read_bytes())
        rows = {r.values["StageId"]: r for r in read(bytes(data))}
        mine, theirs = rows[sid], rows[ref]
        changed = []
        for f in FIELDS:
            if mine.values[f] == theirs.values[f]:
                continue
            if f not in mine.offsets:
                print(f"  {uexp.stem}: {f} not stored in {stage}'s row (zero); left at 0 (reference {theirs.values[f]})")
                continue
            fmt = "<B" if f == "AutoExposureMethod" else "<f"
            struct.pack_into(fmt, data, mine.offsets[f], theirs.values[f])
            changed.append(f"{f} {mine.values[f]:.3g} -> {theirs.values[f]:.3g}")
        out = OUT / TABLES / uexp.name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
        out.with_suffix(".uasset").write_bytes(uexp.with_suffix(".uasset").read_bytes())
        print(f"  {uexp.stem}: {', '.join(changed) or 'unchanged'}")


def patch_intro(stage: str) -> None:
    rel = f"01_Union/Scene/IntroduceCourse/LS_{stage}"
    data = bytearray((SRC / (rel + ".uexp")).read_bytes())
    for off, (old, new) in INTRO_PATCHES.items():
        cur = struct.unpack_from("<f", data, off)[0]
        if abs(cur - old) > 1e-4:
            raise SystemExit(f"LS_{stage} offset {off}: expected {old}, found {cur} (asset changed?)")
        struct.pack_into("<f", data, off, new)
    out = OUT / (rel + ".uexp")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    out.with_suffix(".uasset").write_bytes((SRC / (rel + ".uasset")).read_bytes())
    print(f"  LS_{stage}: {', '.join(f'{o}: {a} -> {b}' for o, (a, b) in INTRO_PATCHES.items())}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--like", default="stg1022")
    args = ap.parse_args()
    patch_tables(args.stage, args.like)
    patch_intro(args.stage)


if __name__ == "__main__":
    main()
