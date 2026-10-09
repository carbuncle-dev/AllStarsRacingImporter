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

from cw.locres import parse_locres, write_locres
from pipeline.music import ROOT, UNREALPAK

SOURCE = ROOT / "extracted" / "cw" / "loc_all" / "UNION" / "Content" / "Localization" / "Game"
MOUNT = "../../../UNION/Content/Localization/Game/"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--title", default="ROCKY COASTER")
    ap.add_argument("--name", default="RockyCoaster")
    args = ap.parse_args()
    key = f"course_name_{args.stage[3:]}"
    out_dir = ROOT / "out" / "text" / "Game"
    lines = []
    for src in sorted(SOURCE.glob("*/Game.locres")):
        loc = parse_locres(src.read_bytes())
        old = loc.text("DB_Course", key)
        loc.set_text("DB_Course", key, args.title)
        dst = out_dir / src.parent.name / "Game.locres"
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(write_locres(loc))
        lines.append(f'"{dst.resolve()}" "{MOUNT}{src.parent.name}/Game.locres"\n')
        print(f"   {src.parent.name}: {old!r} -> {args.title!r}".encode("ascii", "backslashreplace").decode())
    response = out_dir.parent / "files.txt"
    response.write_text("".join(lines))
    pak = ROOT / "out" / "mods" / f"zzz_{args.name}_Text_P.pak"
    subprocess.run([str(UNREALPAK), str(pak), f"-create={response}"], check=True, capture_output=True)
    print(f"wrote {pak} ({len(lines)} languages)")


if __name__ == "__main__":
    main()
