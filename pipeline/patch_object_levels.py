"""Move actors in a stage's shipped object levels by patching the cooked level files in place.

    python -m pipeline.patch_object_levels out/samba_Hard/unreal/stg1016_actors_moved.json \
        extracted/cw/objlevel out/cooked_extra

Re-saving these levels in the custom 1.1.2-era editor dropped components whose classes only exist
in the 1.4.1 game (GimmickStaticBodyComponent -- boost pads' trigger), and rewrote every property.
Instead, the original cooked (unversioned) level is kept byte for byte except for transforms:
a root component stores RelativeLocation (3 doubles) immediately followed by RelativeRotation
(pitch, yaw, roll doubles) when the rotation is not zero. Locations are unique in the file, so
each actor's location is found by its exact bytes and overwritten together with the rotation.
  * actors whose rotation is zero have no stored rotation: only their location is patched
  * actors sharing one location are patched together when they all move to the same spot
  * removed actors ("delete" in the moves file) are sunk far below the course
Attached actors store a location relative to their parent and follow it unchanged.

Get the original levels with:
    retoc -a <AES_KEY> to-legacy --version UE5_4 --no-shaders -f SL_<stage>_Object_Game <Paks> extracted/cw/objlevel
"""

from __future__ import annotations

import argparse
import json
import struct
from collections import defaultdict
from pathlib import Path

SUNK_Z = -200000.0  # 2 km below the course, for removed actors

# Levels that were never dumped in the editor (item boxes, gimmick variants): their actors are read
# straight from the cooked package. The legacy (UE 5.4) export table is a run of 96-byte entries --
# class, super, template, outer (i32 package indices), FName, flags, then i64 SerialSize and
# SerialOffset at +28/+36, consecutive exports' data back to back -- and the name map a run of
# (i32 length, chars + NUL, u32 hash). An actor's root SceneComponent (62 bytes) stores
# RelativeLocation at +6 and RelativeRotation at +30 (38 bytes: location only, zero rotation).
EXPORT_SIZE = 96


def _name_map(umap: bytes) -> list[str]:
    for p in range(8, 3000):
        count, offset = struct.unpack_from("<ii", umap, p)
        if not (5 < count < 100000 and 0 < offset < len(umap)):
            continue
        names, i = [], offset
        try:
            for _ in range(count):
                n = struct.unpack_from("<i", umap, i)[0]
                if not 0 < n < 1024 or umap[i + 3 + n] != 0:
                    raise ValueError
                names.append(umap[i + 4:i + 3 + n].decode("latin1"))
                i += 4 + n + 4
        except (ValueError, struct.error):
            continue
        if any(name.startswith("/") for name in names):
            return names
    raise ValueError("name map not found")


def _export_table(umap: bytes) -> list[tuple[int, int, int]]:
    """(entry offset in .umap, serial size, serial offset in .uexp) for every export."""
    for p in range(len(umap) - EXPORT_SIZE):
        size, offset = struct.unpack_from("<qq", umap, p + 28)
        if offset != len(umap) or not 0 < size < 1 << 24:
            continue
        table, q = [], p
        while q + EXPORT_SIZE <= len(umap):
            size, offset = struct.unpack_from("<qq", umap, q + 28)
            if table and offset != table[-1][2] + table[-1][1] + len(umap) or not 0 <= size < 1 << 28:
                break
            table.append((q, size, offset - len(umap)))
            q += EXPORT_SIZE
        if len(table) > 2:
            return table
    raise ValueError("export table not found")


# A component with no stored properties (actor at the world origin): 4 zero bytes, unversioned header
# 0x012C (skip all, last fragment), 8 zero bytes. With a RelativeLocation it becomes header 0x0305
# (skip 5, 1 value, last) + 3 doubles, like a 38-byte SceneComponent.
EMPTY_ROOT = bytes(4) + b"," + bytes(8)
BULK_DATA_START = 223  # i64 in the package summary: .umap + .uexp size - 4


def sink_unplaced(umap: bytes, uexp: bytes, actors: list[str]) -> tuple[bytes, bytes]:
    """Give the empty Root component of each named actor a RelativeLocation far below the course."""
    names = _name_map(umap)
    table = _export_table(umap)
    exports = [(struct.unpack_from("<iII", umap, q + 12), q, size, offset) for q, size, offset in table]
    label = lambda e: names[e[0][1]] + (f"_{e[0][2] - 1}" if e[0][2] else "")
    total = len(umap) + len(uexp) - 4
    if struct.unpack_from("<q", umap, BULK_DATA_START)[0] != total:
        raise ValueError("unexpected package summary layout")
    umap, uexp = bytearray(umap), bytearray(uexp)
    for actor in actors:
        index = next(i for i, e in enumerate(exports, 1) if label(e) == actor)
        _, q, size, offset = next(e for e in exports if e[0][0] == index and label(e) == "Root")
        cur_off = struct.unpack_from("<q", umap, q + 36)[0] - len(umap)
        if bytes(uexp[cur_off:cur_off + len(EMPTY_ROOT)]) != EMPTY_ROOT:
            raise ValueError(f"{actor}: Root is not empty")
        new = bytes(4) + b"" + struct.pack("<3d", 0.0, 0.0, SUNK_Z) + bytes(8)
        uexp[cur_off:cur_off + len(EMPTY_ROOT)] = new
        grow = len(new) - len(EMPTY_ROOT)
        struct.pack_into("<q", umap, q + 28, size + grow)
        for _, eq, _, _ in exports:
            o = struct.unpack_from("<q", umap, eq + 36)[0]
            if o - len(umap) > cur_off:
                struct.pack_into("<q", umap, eq + 36, o + grow)
        struct.pack_into("<q", umap, BULK_DATA_START, struct.unpack_from("<q", umap, BULK_DATA_START)[0] + grow)
    return bytes(umap), bytes(uexp)


def read_level_actors(umap: bytes, uexp: bytes) -> list[dict]:
    """Actor rows (tools/ue_dump_actors.py format) for actors with a SceneComponent root."""
    names = _name_map(umap)
    exports = []
    for q, size, offset in _export_table(umap):
        outer, name, number = struct.unpack_from("<iII", umap, q + 12)
        exports.append((outer, names[name] + (f"_{number - 1}" if number else ""), size, offset))
    level = next(i + 1 for i, e in enumerate(exports) if e[1] == "PersistentLevel")
    rows = []
    for index, (outer, name, _, _) in enumerate(exports, 1):
        if outer != level:
            continue
        root = next((e for e in exports if e[0] == index and e[1].startswith("SceneComponent")
                     and e[2] in (38, 62)), None)
        if root is None:
            if any(e[0] == index and e[1] == "Root" and e[2] == len(EMPTY_ROOT) for e in exports):
                rows.append({"name": name, "label": name, "class": name.rsplit("_", 1)[0], "location": [0.0] * 3,
                             "rotation": [0.0] * 3, "scale": [1.0] * 3, "attached": False, "unplaced": True})
            continue
        location = list(struct.unpack_from("<3d", uexp, root[3] + 6))
        rotation = list(struct.unpack_from("<3d", uexp, root[3] + 30)) if root[2] == 62 else [0.0, 0.0, 0.0]
        rows.append({"name": name, "label": name, "class": name.rsplit("_", 1)[0],
                     "location": location, "rotation": rotation, "scale": [1.0, 1.0, 1.0], "attached": False})
    return rows


def patch_level(data: bytes, rows: list[dict]) -> tuple[bytes, dict]:
    out = bytearray(data)
    stats = defaultdict(int)
    groups = defaultdict(list)
    for r in rows:
        groups[struct.pack("<3d", *r["from"])].append(r)
    for key, members in groups.items():
        targets = {tuple(round(v, 3) for v in (r["location"] if not r.get("delete") else
                   (r["from"][0], r["from"][1], SUNK_Z))) for r in members}
        hits = []
        start = 0
        while (i := data.find(key, start)) >= 0:
            hits.append(i)
            start = i + 1
        if len(targets) != 1 or len(hits) != len(members):
            stats["skipped (shared location, different targets or count mismatch)"] += len(members)
            continue
        new_loc = next(iter(targets))
        for off, r in zip(hits, members):
            struct.pack_into("<3d", out, off, *new_loc)
            rot = r["from_rotation"]
            stored = struct.unpack_from("<3d", data, off + 24)
            same = all(abs(((a - b) + 180.0) % 360.0 - 180.0) < 1e-3 for a, b in zip(stored, rot))
            if r.get("delete"):
                stats["sunk"] += 1
            elif same and any(abs(v) > 1e-9 for v in rot):
                struct.pack_into("<3d", out, off + 24, *r["rotation"])
                stats["moved + rotated"] += 1
            else:
                stats["moved (no stored rotation)"] += 1
    return bytes(out), dict(stats)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("moves", type=Path, help="pipeline.retarget_actors output")
    ap.add_argument("original", type=Path, help="folder with the legacy-extracted original levels (UNION/Content/...)")
    ap.add_argument("out", type=Path, help="staging folder (UNION/Content/... is created inside)")
    args = ap.parse_args()
    moves = json.loads(args.moves.read_text())
    for level, rows in moves.items():
        rel = Path("UNION/Content") / level.replace("/Game/", "")
        src = args.original / rel
        uexp = Path(str(src) + ".uexp")
        if not uexp.exists():
            raise SystemExit(f"missing {uexp}; extract the original levels first (see module docstring)")
        unplaced = [r["name"] for r in rows if r.get("unplaced") and r.get("delete")]
        patched, stats = patch_level(uexp.read_bytes(), [r for r in rows if not r.get("unplaced")])
        umap = Path(str(src) + ".umap").read_bytes()
        if unplaced:
            umap, patched = sink_unplaced(umap, patched, unplaced)
            stats["sunk (location added)"] = len(unplaced)
        dst = args.out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        Path(str(dst) + ".uexp").write_bytes(patched)
        Path(str(dst) + ".umap").write_bytes(umap)
        print(f"{level.split('/')[-1]}: {stats}")


if __name__ == "__main__":
    main()
