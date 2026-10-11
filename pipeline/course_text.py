"""Rename the replaced course in the game's text tables (course select, loading screen, results).

    python -m pipeline.course_text --stage stg1016 --title "ROCKY COASTER" --name RockyCoaster

Edits DB_Course/course_name_<id> in every language's Game.locres (loose files in pakchunk0, mounted at
UNION/Content/Localization/Game/<lang>/) with cw.locres -- strings shared with other keys (a sticker
label in some languages) are split off, so only the course name changes -- and packs them with
UnrealPak into out/mods/zzz_<name>_Text_P.pak.

Extract the originals first:
    UnrealPak <Paks>/pakchunk0-Windows.pak -Extract <abs>/extracted/cw/loc_all -ExtractToMountPoint
        -cryptokeys=<crypto.json> -Filter="*Localization/Game/*"
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from cw.locres import add_text, parse_locres, write_locres
from pipeline import announcer
from pipeline.music import ROOT, UNREALPAK

SOURCE = ROOT / "extracted" / "cw" / "loc_all" / "UNION" / "Content" / "Localization" / "Game"
STICKER = {"stg1016": "UI_Sticker_10052"}  # DB_Sticker key of the course's sticker (its localised name)
MOUNT = "../../../UNION/Content/Localization/Game/"
# Names of the unreleased debug stages the custom Grand Prix uses (CDT_StageDataAsset rows: localisable literal
# texts in the empty namespace, key -> source string). A locres entry only applies when its source hash matches.
DEBUG_STAGE_TEXT = {3001: ("4EF07D8F4055E11D97B7E9B646AEC336", "DebugMain01"),
                    3002: ("AA39A1C74B4FE6563FC1E19192FA428D", "DebugMain02"),
                    3003: ("CDD11CF241790325DDB7FABA0B9DFD7C", "DebugMain03")}
# The custom cup's name (DT_GrandPrixStages, Collabo_03 row: a literal text, empty namespace; the stock cups
# use DB_Course string-table entries instead). Shown in the course-select name bubble and the Grand Prix screens.
CUP_TEXT = ("318714AE47121991DD01BCAD8A75A99C", "コラボグランプリ３")


def name_new_stages(stages: list[int], title: str, name: str, cup: str = "CUSTOM GRAND PRIX") -> None:
    """Text pak for a track shipped as new stage ids: the stock stage's texts stay as they are; the new
    stages' names (placeholders "DebugMain01"...) get the track's name in every language."""
    out_dir = ROOT / "out" / "text" / "Game"
    lines = []
    for src in sorted(SOURCE.glob("*/Game.locres")):
        loc = parse_locres(src.read_bytes())
        for stage in stages:
            key, source = DEBUG_STAGE_TEXT[stage]
            add_text(loc, "", key, source, title)
        add_text(loc, "", CUP_TEXT[0], CUP_TEXT[1], cup)
        dst = out_dir / src.parent.name / "Game.locres"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(write_locres(loc))
        lines.append(f'"{dst.resolve()}" "{MOUNT}{src.parent.name}/Game.locres"\n')
    response = out_dir.parent / "files.txt"
    response.write_text("".join(lines))
    pak = ROOT / "out" / "mods" / f"zzz_{name}_Text_P.pak"
    subprocess.run([str(UNREALPAK), str(pak), f"-create={response}"], check=True, capture_output=True)
    print(f"wrote {pak}: stages {stages} named {title!r}, cup {cup!r}, in {len(lines)} languages")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--title", default="ROCKY COASTER")
    ap.add_argument("--name", default="RockyCoaster")
    ap.add_argument("--new-stages", help="the track ships as these new stage ids (e.g. 3001,3002,3003): name "
                    "them, and leave the stock stage's texts alone")
    args = ap.parse_args()
    if args.new_stages:
        name_new_stages([int(x) for x in args.new_stages.split(",")], args.title, args.name)
        return
    key = f"course_name_{args.stage[3:]}"
    out_dir = ROOT / "out" / "text" / "Game"
    lines = []
    sid = args.stage[3:]
    for src in sorted(SOURCE.glob("*/Game.locres")):
        loc = parse_locres(src.read_bytes())
        old = loc.text("DB_Course", key)
        loc.set_text("DB_Course", key, args.title)
        # Other texts naming the stage: activity titles ("E-Stadium (Sonic Speed)"), music names (the
        # stage's BGM now plays Rocky Coaster's), and the announcer's course-call subtitle. The stage's
        # localised name is taken from the course name and the course sticker ("E-スタジアム", "Э-стадион").
        # Left alone: the course sticker (its picture is E-Stadium's) and characters' course comments
        # (spoken lines about E-Stadium).
        names = {old, loc.text("DB_Sticker", STICKER.get(args.stage, "")) if STICKER.get(args.stage) else old,
                 "E-Stadium", "E-STADIUM"}
        names |= {n.title() for n in names} | {n.upper() for n in names}
        changed = []
        for ns, k in (("Activity", f"STG{sid}_SonicSpeed"), ("Activity", f"STG{sid}_SuperSonicSpeed"),
                      ("DB_BGM", f"bgm_name_{sid}0"), ("DB_BGM", f"bgm_name_{sid}1"), ("DB_BGM", f"bgm_name_{sid}2")):
            try:
                text = loc.text(ns, k)
            except KeyError:
                continue
            new = text
            for n in sorted(names, key=len, reverse=True):
                if n and n in new:
                    shouty = n.isupper() and sum(c.isalpha() and c.isascii() for c in n) > 1  # "E-STADIUM"
                    new = new.replace(n, args.title if shouty else args.title.title())
            if new != text:
                loc.set_text(ns, k, new)
                changed.append(k)
        # the announcer now says the generic "Special Track" (pipeline.announcer): matching subtitle
        ann = "DB_CV901_GenericAnnouncer"
        loc.set_text(ann, f"voice_announce_coursecall_{args.stage}", loc.text(ann, announcer.REPLACEMENT))
        dst = out_dir / src.parent.name / "Game.locres"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(write_locres(loc))
        lines.append(f'"{dst.resolve()}" "{MOUNT}{src.parent.name}/Game.locres"\n')
        print(f"   {src.parent.name}: {old!r} -> {args.title!r}; also {len(changed)} texts".encode("ascii", "backslashreplace").decode())
    for local, rel in announcer.build(args.stage):
        lines.append(f'"{local.resolve()}" "../../../UNION/Content/{rel}"\n')
    response = out_dir.parent / "files.txt"
    response.write_text("".join(lines))
    pak = ROOT / "out" / "mods" / f"zzz_{args.name}_Text_P.pak"
    subprocess.run([str(UNREALPAK), str(pak), f"-create={response}"], check=True, capture_output=True)
    print(f"wrote {pak} ({len(lines)} files)")


if __name__ == "__main__":
    main()
