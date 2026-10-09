"""Write the track's placed objects (ASR logic locators) for the Unreal export.

    python -m pipeline.asr_objects extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/Samba_Hard.obj

Writes <obj>.locators.json: per locator the item.Forest tree index it shows, its ASR-space position and
rotation quaternion (x, y, z, w) and playback speed. export_for_unreal places a copy of that tree there
(animated: the tree's looping "Auto" animation). Item boxes are skipped (CrossWorlds has its own).
The tree index comes from forest_tree_dump's index (tree hashes are not in forest_to_obj's output).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from asr.logic import parse_logic
from asr.sif import read_sif

ROOT = Path(__file__).resolve().parent.parent
TREE_DUMP = ROOT / "build" / "cppslib" / "Release" / "forest_tree_dump.exe"
SKIP_TREES = {0, 3}  # item.Forest 0: item box (CrossWorlds has its own); 3: a pad-like panel on the rainbow slide that does nothing here


def item_tree_hashes(sif: Path) -> dict[int, int]:
    """unsigned tree hash -> tree index within item.Forest"""
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([str(TREE_DUMP), str(sif), tmp], check=True, capture_output=True)
        index = (Path(tmp) / "index.txt").read_text()
    out, forest = {}, None
    for line in index.splitlines():
        m = re.match(r"Forest\[\d+\] name=(\S+)", line)
        if m:
            forest = m.group(1)
        m = re.search(r"Tree\[(\d+)\] dir=tree_\d+_hash_(-?\d+)", line)
        if m and forest == "item.Forest":
            out[int(m.group(2)) & 0xFFFFFFFF] = int(m.group(1))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sif", type=Path)
    ap.add_argument("obj", type=Path)
    args = ap.parse_args()
    trees = item_tree_hashes(args.sif)
    locs = parse_logic(read_sif(args.sif)["LOGC"])
    rows = []
    for loc in locs:
        tree = trees.get(loc.tree)
        if tree is None or tree in SKIP_TREES:
            continue
        rows.append({"tree": tree, "position": list(loc.position), "rotation": list(loc.rotation),
                     "playback_speed": loc.playback_speed, "flags": loc.flags,
                     "start": loc.start_frame_multiplier})  # fraction of the loop to start at
    out = args.obj.with_suffix(".locators.json")
    out.write_text(json.dumps(rows, indent=1))
    print(f"{len(rows)} placed objects (of {len(locs)} locators): {dict(Counter(r['tree'] for r in rows))} -> {out}")


if __name__ == "__main__":
    main()
