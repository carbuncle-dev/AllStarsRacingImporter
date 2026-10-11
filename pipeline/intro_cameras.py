"""Move the stage-intro fly-over cameras (Scene/IntroduceCourse/LS_<stage>) onto the new track.

Rocky Coaster's data has no intro camera paths (its cameras.Forest holds replay-camera trigger spheres
only), so E-Stadium's shots are kept and retargeted: every transform section whose keys lie near the
template's route (E-Stadium: the high sweep over the start and the low shot near the finish) has each
key pose mapped like the placed actors (retarget_actors.map_pose: same lap position, sideways offset,
height and orientation relative to the road). Sections far from the route (the distant establishing
shot over the clouds, which already frames our course, and helper objects) are left alone.
Patched in place over the file pipeline.scene_settings wrote (out/ui_cooked), keys only.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np

from cw.collision import parse_collision
from cw.movie_scene import find_channels, set_key, transform_sections, value_at
from pipeline.asr_to_cw import START_SHIFTS, TEMPLATE_DIR, drivable_triangles
from pipeline.retarget_actors import Route, map_pose, ue_to_cw
from pipeline.scene_settings import OUT, SRC

MAX_PAN = 22.0     # degrees of yaw change within a shot
NEAR_ROUTE = 150.0  # m: sections with every key this close to the template route are retargeted


def retarget(stage: str, track_dir: Path) -> None:
    rel = f"01_Union/Scene/IntroduceCourse/LS_{stage}.uexp"
    path = OUT / rel if (OUT / rel).exists() else SRC / rel
    data = bytearray(path.read_bytes())
    t_road = drivable_triangles(parse_collision((TEMPLATE_DIR / f"{stage}_Lap_Collision.bin").read_bytes()))
    n_road = drivable_triangles(parse_collision((track_dir / f"cw_{stage}" / "Lap_Collision.bin").read_bytes()))
    src = Route((TEMPLATE_DIR / f"{stage}_Lap_Navigation.bin").read_bytes(), t_road, origin=START_SHIFTS.get(stage, 0.0))
    dst = Route((track_dir / f"cw_{stage}" / "Lap_Navigation.bin").read_bytes(), n_road)
    for sec in transform_sections(find_channels(bytes(data))):
        loc, rot = sec[0:3], sec[3:6]
        times = sorted({t for c in loc + rot for t in c.times if t >= 0})
        poses = [([value_at(c, t) for c in loc], [value_at(c, t) for c in rot]) for t in times]
        dist = [float(np.min(np.linalg.norm(src.pos - ue_to_cw(p), axis=1))) for p, _ in poses]
        if not poses or max(dist) > NEAR_ROUTE:
            print(f"   section @{sec[0].offset}: {len(times)} keys, {max(dist, default=0):.0f} m from the route: kept")
            continue
        mapped = [map_pose(p, (pitch, yaw, roll), src, dst) for p, (roll, pitch, yaw) in poses]
        # Our road is narrower and closer to scenery: a wide pan that swept E-Stadium's stands started
        # on a wall here. Pans are limited to MAX_PAN degrees, ending where the original ends.
        end_yaw = mapped[-1][1][1]
        for i, (p2, (pitch2, yaw2, roll2)) in enumerate(mapped):
            d = (yaw2 - end_yaw + 180.0) % 360.0 - 180.0
            if abs(d) > MAX_PAN:
                mapped[i] = (p2, (pitch2, end_yaw + np.sign(d) * MAX_PAN, roll2))
        for t, (p2, (pitch2, yaw2, roll2)) in zip(times, mapped):
            for c, v in zip(loc + rot, list(p2) + [roll2, pitch2, yaw2]):
                set_key(data, c, t, v)
            # keys at -1 (before the section) mirror the first pose
            for c, v in zip(loc + rot, list(p2) + [roll2, pitch2, yaw2]):
                if t == times[0]:
                    set_key(data, c, -1, v)
        print(f"   section @{sec[0].offset}: {len(times)} keys retargeted "
              f"({np.round(poses[0][0]).astype(int).tolist()} -> {np.round(map_pose(*poses[0][:1], (poses[0][1][1], poses[0][1][2], poses[0][1][0]), src, dst)[0]).astype(int).tolist()})")
    dst_path = OUT / rel
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    dst_path.write_bytes(bytes(data))
    asset = (SRC / rel).with_suffix(".uasset")
    if not dst_path.with_suffix(".uasset").exists():
        dst_path.with_suffix(".uasset").write_bytes(asset.read_bytes())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("track_dir", type=Path)
    ap.add_argument("--stage", default="stg1016")
    args = ap.parse_args()
    retarget(args.stage, args.track_dir)


if __name__ == "__main__":
    main()
