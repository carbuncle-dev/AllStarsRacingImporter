"""Export an ASR track's gameplay data (collision + navigation) to JSON.

    python -m pipeline.export_track extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/gameplay.json

Coordinates are left in game space (Y up); consumers convert as needed.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path

from asr.collision import parse_collision
from asr.navigation import parse_navigation
from asr.sif import read_sif


def r(v, nd=3):
    return [round(float(x), nd) for x in v]


def export(sif_path: Path) -> dict:
    chunks = read_sif(sif_path)
    col = parse_collision(chunks["COLI"])
    nav = parse_navigation(chunks["TRAK"])

    surfaces = {}
    for h in sorted(set(int(s) for s in col.surfaces)):
        mask = col.surfaces == h
        surfaces[col.surface_name(h)] = {
            "hash": h,
            "triangles": col.triangles[mask].tolist(),
            "flags": sorted(collections.Counter(int(f) for f in col.flags[mask]).items()),
        }

    link_ids = {off: i for i, off in enumerate(nav.links)}
    return {
        "source": str(sif_path),
        "coordinate_system": "game (Y up, meters-ish units)",
        "collision": {
            "name": col.name,
            "vertices": [r(v) for v in col.vertices],
            "surfaces": surfaces,
        },
        "navigation": {
            "version": nav.version,
            "total_track_dist": nav.total_track_dist,
            "waypoints": [{
                "name": w.name, "pos": r(w.pos), "dir": r(w.dir, 4), "up": r(w.up, 4),
                "track_dist": round(w.track_dist, 3), "flags": w.flags, "target_speed": w.target_speed,
                "to_links": [link_ids[o] for o in w.to_links],
            } for w in nav.waypoints],
            "links": [{
                "from": l.from_wp, "to": l.to_wp, "left": r(l.left), "right": r(l.right),
                "racing_line_limit_left": r(l.racing_line_limit_left),
                "racing_line_limit_right": r(l.racing_line_limit_right),
                "width": round(l.width, 3), "length": round(l.length, 3),
                "cross_section": [r(p) for p in l.cross_section],
            } for l in nav.links.values()],
            "racing_lines": [{
                "looping": rl.looping, "total_length": rl.total_length,
                "points": [r(s.racing_line) for s in rl.segments],
                "safe_points": [r(s.safe_racing_line) for s in rl.segments],
                "turn_types": [s.turn_type for s in rl.segments],
            } for rl in nav.racing_lines],
            "track_markers": [{
                "type": m.type, "value": m.value, "pos": r(m.pos), "dir": r(m.dir, 4), "up": r(m.up, 4),
                "radius": round(m.radius, 3), "waypoint": m.waypoint, "track_dist": round(m.track_dist, 3),
                "linked_marker": m.linked_marker, "jump_speed_percentage": m.jump_speed_percentage,
                "flags": m.flags, "text": m.text,
            } for m in nav.track_markers],
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sif", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    data = export(args.sif)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data))
    nav = data["navigation"]
    print(f"collision: {len(data['collision']['vertices'])} verts, "
          + ", ".join(f"{k}={len(v['triangles'])}" for k, v in data["collision"]["surfaces"].items()))
    print(f"navigation: {len(nav['waypoints'])} waypoints, {len(nav['links'])} links, "
          f"{len(nav['track_markers'])} markers, length {nav['total_track_dist']:.1f}")


if __name__ == "__main__":
    main()
