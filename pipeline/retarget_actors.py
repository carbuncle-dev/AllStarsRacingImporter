"""Move a replaced stage's placed actors (travel ring, rings, dash panels, ...) onto the new track.

    python -m pipeline.retarget_actors out/samba_Hard/unreal/stg1016_actors.json out/samba_Hard/cw_stg1016 \
        --template stg1016 -o out/samba_Hard/unreal/stg1016_actors_moved.json [--levels extracted/cw/objlevel]

--levels also reads the stage's other object levels straight from the cooked packages
(pipeline.patch_object_levels.read_level_actors): item boxes are moved like the other actors,
the moving dash panels of the Irregular gimmick level (on splines laid along the old course) sunk.

Each actor is expressed relative to the template's main route -- distance along the lap,
sideways offset, height above the route and heading relative to the road -- and rebuilt at
the same relative spot on the converted route:
  * distance: kept absolute within FIXED_RANGE of the start (both routes share the start and
    finish placement, see pipeline.asr_to_cw --finish-offset), proportional elsewhere
  * sideways: on-road actors keep their fraction of the road half-width; actors beside the
    road keep their distance from the road edge
Unreal <-> CW collision space: UE (X, Y, Z) = (z, -x, y) * 100.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from cw.collision import parse_collision
from cw.navigation import parse_navigation
from pipeline.asr_to_cw import START_SHIFTS, TEMPLATE_DIR, drivable_triangles, main_route, road_extent

FIXED_RANGE = 300.0


def ue_to_cw(v):
    x, y, z = v
    return np.array([-y, z, x]) / 100.0


def cw_to_ue(v):
    x, y, z = v
    return [z * 100.0, -x * 100.0, y * 100.0]


def ue_yaw(d_cw):
    x, y, z = d_cw
    return math.degrees(math.atan2(-x, z))  # UE dir = (z, -x, y)



# --- Unreal rotator <-> matrix (FRotationMatrix / FMatrix::Rotator conventions) -------------
# Columns are the rotated X (forward), Y (right) and Z (up) axes in Unreal world space.

def rotator_to_matrix(pitch, yaw, roll):
    p, y, r = (math.radians(v) for v in (pitch, yaw, roll))
    sp, cp, sy, cy, sr, cr = math.sin(p), math.cos(p), math.sin(y), math.cos(y), math.sin(r), math.cos(r)
    x_axis = [cp * cy, cp * sy, sp]
    y_axis = [sr * sp * cy - cr * sy, sr * sp * sy + cr * cy, -sr * cp]
    z_axis = [-(cr * sp * cy + sr * sy), cy * sr - cr * sp * sy, cr * cp]
    return np.array([x_axis, y_axis, z_axis]).T


def matrix_to_rotator(m):
    x, y_axis, z_axis = m[:, 0], m[:, 1], m[:, 2]
    pitch = math.degrees(math.atan2(x[2], math.hypot(x[0], x[1])))
    yaw = math.degrees(math.atan2(x[1], x[0]))
    sy_axis = rotator_to_matrix(pitch, yaw, 0.0)[:, 1]
    roll = math.degrees(math.atan2(z_axis @ sy_axis, y_axis @ sy_axis))
    return [pitch, yaw, roll]


CW_TO_UE = np.array([[0.0, 0, 1], [-1, 0, 0], [0, 1, 0]])  # direction mapping, UE = (z, -x, y)


def road_basis(d, u, lat):
    """Orthonormal road frame (forward, left, up) as Unreal-space columns."""
    f = CW_TO_UE @ d
    up = CW_TO_UE @ u
    up = up - f * (up @ f)
    up /= np.linalg.norm(up)
    left = np.cross(up, f)  # completes the frame consistently for both routes
    return np.column_stack([f, left, up])


class Route:
    def __init__(self, nav_bytes: bytes, road: np.ndarray, origin: float = 0.0):
        """`origin`: lap distance (from travel index 0) that this route's s = 0 should be."""
        nav = parse_navigation(nav_bytes)
        order = main_route(nav)
        self.pos = np.array([nav.waypoints[i].pos for i in order], np.float64)
        self.up = np.array([nav.waypoints[i].up for i in order], np.float64)
        seg = np.linalg.norm(np.diff(np.vstack([self.pos, self.pos[:1]]), axis=0), axis=1)
        s = np.concatenate([[0], np.cumsum(seg)])[:-1]
        self.length = float(seg.sum())
        if origin:
            k0 = int(np.searchsorted(s, origin % self.length))
            self.pos, self.up, seg = np.roll(self.pos, -k0, 0), np.roll(self.up, -k0, 0), np.roll(seg, -k0)
            s = (s[k0 % len(s)] - origin % self.length) + np.concatenate([[0], np.cumsum(seg)])[:-1]
        self.s = s
        nxt = np.roll(self.pos, -1, 0) - self.pos
        self.dir = nxt / np.linalg.norm(nxt, axis=1, keepdims=True)
        self.road = road

    def frame(self, k):
        lat = np.cross(self.up[k], self.dir[k])
        return self.pos[k], self.dir[k], self.up[k], lat / np.linalg.norm(lat)

    def locate(self, p, yaw=None, slack=10.0):
        """Route frame for point p. Where the route passes close by more than once (crossings,
        parallel straights), prefer the pass whose heading best matches the actor's yaw."""
        dist = np.linalg.norm(self.pos - p, axis=1)
        k = int(np.argmin(dist))
        if yaw is not None:
            near = np.flatnonzero(dist <= dist[k] + slack)
            heading = np.array([ue_yaw(self.dir[i]) for i in near])
            diff = np.abs((heading - yaw + 180.0) % 360.0 - 180.0)
            k = int(near[np.argmin(diff + dist[near] * 0.5)])
        o, d, u, lat = self.frame(k)
        rel = p - o
        return (self.s[k] + rel @ d) % self.length, rel @ lat, rel @ u, k

    def at(self, s):
        s %= self.length
        k = int(np.clip(np.searchsorted(self.s, s) - 1, 0, len(self.pos) - 1))
        o, d, u, lat = self.frame(k)
        return o + d * (s - self.s[k]), d, u, lat


def surface_hit(p, up, road, reach=8.0):
    """Closest road hit along the line through p in direction up (|distance| <= reach):
    (point, triangle normal facing up) or None."""
    a, e1, e2 = road[:, 0], road[:, 1] - road[:, 0], road[:, 2] - road[:, 0]
    h = np.cross(up, e2)
    det = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(det) > 1e-9
    f = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
    sv = p - a
    u = f * np.einsum("ij,ij->i", sv, h)
    q = np.cross(sv, e1)
    v = f * (q @ up)
    t = -f * np.einsum("ij,ij->i", e2, q)  # signed distance of p above the hit, along up
    m = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (np.abs(t) <= reach)
    if not m.any():
        return None
    i = np.flatnonzero(m)[np.argmin(np.abs(t[m]))]
    n = np.cross(e1[i], e2[i])
    n /= np.linalg.norm(n)
    if n @ up < 0:
        n = -n
    return p - up * t[i], n


# Stadium gimmicks with no counterpart on an ASR track: guided jump stands launch karts toward a
# landing spot that only exists on the template, so moved onto our road they throw karts off it.
# Static meshes and line guides are E-Stadium set dressing (character panels, goal glow, the final
# lap's road-jump add-on and its blocker cubes, guide arrows) placed for its own layout.
# Bollards (posts) line E-Stadium's run-off areas; on our road they stand on the racing line.
# Rubber-tyre stacks stand on a wide apron beside E-Stadium's racing line; on a narrower road
# they end up as solid obstacles in the middle of it.
# BP_Respot volumes are E-Stadium's fall zones: entering one teleports the kart to a fixed respawn
# transform (ARespotObject.RespotMatrix). Moved onto our road they would fire on solid track.
# Object levels not in the editor dump: suffix -> True to move their actors, False to sink them all.
EXTRA_LEVELS = {"_Object_ItemBox": True, "_Object_ItemBox_FinalLap": True, "_Object_ItemBox_Irregular": True,
                "_Object_Game_Irregular": False,
                "_FX": False}  # travel-ring floor: world-space meshes shaped for E-Stadium's road
REMOVED_CLASSES = {"BP_GuidedJumpStand_C", "BP_Respot_C", "BP_Bollard_C", "StaticMeshActor", "BP_LineGuide_C"}
REMOVED_LABELS = ("SM_1000_ks1_RubberTyres",)
# Pickups the removed jump stands launched racers into (high above the road), or that end up away
# from our road with nothing under them (beside E-Stadium's wider course), would float out of reach.
AIR_PICKUPS = {"BP_PackageRing_C", "BP_RedStarRing_C", "BP_FX_ShortcutRing_C", "BP_ItemBox_C"}
AIR_HEIGHT = 5.0     # m above the template road
OFF_ROAD = 15.0      # m from the new route with no road below
PAD_CLASSES = {"BP_DashPanel_C"}
PAD_EDGE_CLEARANCE = 5.0  # m from a boost pad's centre to the road edge (pad ~2 m + a kart's width)
PAD_DECK = 2.0            # m: road further below the pad than this is another deck, not its width
# Boost pads rest on the VISIBLE track (pipeline.visual_road): the collision surface they were snapped to
# lies up to ~0.5 m off the render mesh in places (raised sleepers, steeper banking), which hid parts of
# them. Their footprint is sampled, a plane fitted to the visible surface under it gives the tilt, and
# the pad is lifted by whatever still rises above that plane; it keeps its template height above it.
PAD_HALF = (2.5, 1.75)    # m, half length / width of the footprint checked (covers the wider lap-3 pads)
PAD_SAMPLES = (11, 8)
PAD_MAX_LIFT = 0.15       # m; a bigger lift (raised deck section under part of the pad) would leave the
PAD_SHIFT = 8             # pad's boost trigger above the driving surface: move it up to this many metres
                          # along the road instead, to the nearest spot where the surface is even
SURFACE_SNAP = 3.0
ROAD_SEARCH = 40.0   # m, how far from the mapped spot an on-road actor looks for our road
WORLD_UP = np.array([0.0, 1.0, 0.0])   # actors within this height of the template road keep their height above the surface


def _unit(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


def fit_pad(p2, rot, height, visual):
    """Pad at p2 with rotation matrix rot (CW basis: forward, right, up columns) resting `height` above
    the visible surface under its footprint: (new p2, new rot) or None if no visible surface is found."""
    from pipeline.visual_road import heights_above
    fwd, right, up = rot[:, 0], rot[:, 1], rot[:, 2]
    near = visual[np.linalg.norm(visual.mean(1) - p2, axis=1) < 3 * max(PAD_HALF)]
    grid = [(a, b) for a in np.linspace(-PAD_HALF[0], PAD_HALF[0], PAD_SAMPLES[0])
            for b in np.linspace(-PAD_HALF[1], PAD_HALF[1], PAD_SAMPLES[1])]
    base = p2 - up * height
    t = heights_above(near, np.array([base + fwd * a + right * b for a, b in grid]), up, window=(-1.0, 1.0))
    ok = np.isfinite(t)
    if ok.sum() < len(grid) // 2:
        return None
    ab = np.array(grid)[ok]
    # visible surface heights over the footprint: t = c + ga * a + gb * b (least squares)
    c, ga, gb = np.linalg.lstsq(np.c_[np.ones(len(ab)), ab], t[ok], rcond=None)[0]
    n = up - ga * fwd - gb * right
    n /= np.linalg.norm(n)
    f2 = fwd - n * (fwd @ n)
    f2 /= np.linalg.norm(f2)
    r2 = np.cross(n, f2) if np.cross(n, f2) @ right > 0 else -np.cross(n, f2)
    lift = max(0.0, float(np.max(t[ok] - (c + ab @ np.array([ga, gb])))))  # bumps above the fitted plane
    centre = base + up * c
    return centre + n * (height + lift), np.c_[f2, r2, n], lift


def retarget(actors: dict, src: Route, dst: Route, visual=None) -> dict:
    out = {}
    for level, rows in actors.items():
        moved = []
        for a in rows:
            if a["attached"] or (a.get("unplaced") and not a.get("delete")):
                continue  # follows its parent / stays at the origin
            if a.get("delete") or a["class"] in REMOVED_CLASSES or a["label"].startswith(REMOVED_LABELS):
                moved.append({"name": a["name"], "label": a["label"], "class": a["class"], "delete": True,
                              **({"unplaced": True} if a.get("unplaced") else {}),
                              "from": a["location"], "location": a["location"], "rotation": a["rotation"],
                              "from_rotation": a["rotation"], "lap_distance": [0.0, 0.0]})
                continue
            p = ue_to_cw(a["location"])
            s, lat, h, k = src.locate(p, a["rotation"][1])
            if s <= FIXED_RANGE:
                s2 = s
            elif src.length - s <= FIXED_RANGE:
                s2 = dst.length - (src.length - s)
            else:
                s2 = s * dst.length / src.length
            o2, d2, u2, lat2 = dst.at(s2)
            _, d1, u1, latv1 = src.frame(k)
            side = 0 if lat >= 0 else 1
            e1 = road_extent(src.road, src.pos[k], latv1, u1)[side]
            o2_k = int(np.argmin(np.linalg.norm(dst.pos - o2, axis=1)))
            e2 = road_extent(dst.road, dst.pos[o2_k], lat2, u2)[side]
            if e1 > 1.0 and abs(lat) <= e1:
                new_lat = math.copysign(abs(lat) / e1 * e2, lat)
            else:
                new_lat = math.copysign(e2 + (abs(lat) - e1), lat)
            if a["class"] in PAD_CLASSES:
                # Keep room for the pad and a kart between it and the wall (our road is narrower than
                # E-Stadium's in places, and our route is not always centred on it). Width is measured
                # on the surface the pad sits on only (a deck below once counted as road).
                left, right = road_extent(dst.road, o2 + u2 * h, lat2, u2, depth=PAD_DECK)
                lo, hi = -right + PAD_EDGE_CLEARANCE, left - PAD_EDGE_CLEARANCE
                new_lat = (lo + hi) / 2 if lo > hi else min(max(new_lat, lo), hi)
            p2 = o2 + lat2 * new_lat + u2 * h
            # On-road actors (rings, dash panels, ...): measure height and tilt against the actual
            # road surface under them, not the route's centre line -- our road is banked and
            # cambered, which left dash panels buried or floating and rings out of reach.
            if a["class"] in AIR_PICKUPS:
                below = surface_hit(p, u1, src.road, reach=60.0)
                high = below is not None and (p - below[0]) @ below[1] > AIR_HEIGHT
                stray = surface_hit(p2, u2, dst.road, reach=60.0) is None and np.linalg.norm(p2 - o2) > OFF_ROAD
                if high or stray:
                    moved.append({"name": a["name"], "label": a["label"], "class": a["class"], "delete": True,
                                  "from": a["location"], "location": a["location"], "rotation": a["rotation"],
                                  "from_rotation": a["rotation"], "lap_distance": [round(s, 1), round(s2, 1)]})
                    continue
            n1 = n2 = None
            hit1 = surface_hit(p, u1, src.road)
            if hit1 is not None and abs((p - hit1[0]) @ hit1[1]) <= SURFACE_SNAP:
                hit2 = surface_hit(p2, u2, dst.road)
                # Where our route runs through the air (jumps) the road is further below the
                # mapped spot: look further, straight down too, and sink actors with no road under them.
                for up, reach in ((u2, ROAD_SEARCH), (WORLD_UP, ROAD_SEARCH)):
                    if hit2 is None:
                        hit2 = surface_hit(p2, up, dst.road, reach=reach)
                if hit2 is None:
                    moved.append({"name": a["name"], "label": a["label"], "class": a["class"], "delete": True,
                                  "from": a["location"], "location": a["location"], "rotation": a["rotation"],
                                  "from_rotation": a["rotation"], "lap_distance": [round(s, 1), round(s2, 1)]})
                    continue
                n1, n2 = hit1[1], hit2[1]
                p2 = hit2[0] + n2 * ((p - hit1[0]) @ n1)
            # Keep the actor's full orientation relative to the road surface (slope, bank, heading).
            rot = rotator_to_matrix(*a["rotation"])
            rel = road_basis(d1, n1 if n1 is not None else u1, latv1).T @ rot
            new_rot = road_basis(d2, n2 if n2 is not None else u2, lat2) @ rel
            if a["class"] in PAD_CLASSES and visual is not None and n1 is not None:
                cw_rot = np.column_stack([_unit(ue_to_cw(new_rot[:, k])) for k in range(3)])
                height = (p - hit1[0]) @ n1
                fit = None
                for shift in sorted(range(-PAD_SHIFT, PAD_SHIFT + 1), key=abs):
                    q = p2 + d2 * shift
                    hit = surface_hit(q, n2, dst.road)
                    if hit is None:
                        continue
                    f = fit_pad(hit[0] + hit[1] * height, cw_rot, height, visual)
                    if f is not None and (fit is None or f[2] < fit[2] - 1e-6):
                        fit = f + (shift,)
                    if fit is not None and fit[2] <= PAD_MAX_LIFT:
                        break
                if fit is not None:
                    p2, cw_rot = fit[0], fit[1]
                    new_rot = np.column_stack([_unit(cw_to_ue(cw_rot[:, k])) for k in range(3)])
                    if fit[3] or fit[2] > 0.05:
                        print(f"   pad {a['label']}: moved {fit[3]:+d} m along the road, lifted {fit[2]:.2f} m")
            moved.append({"name": a["name"], "label": a["label"], "class": a["class"],
                          "location": cw_to_ue(p2), "rotation": matrix_to_rotator(new_rot),
                          "from_rotation": a["rotation"],
                          "from": a["location"], "lap_distance": [round(s, 1), round(s2, 1)]})
        out[level] = thin_item_boxes(moved)
    return out


ITEM_BOX_GAP = 3.0   # m; E-Stadium rows are 4 m apart, 4 m left room unused on our road


def thin_item_boxes(moved: list[dict]) -> list[dict]:
    """Sink item boxes squeezed closer than ITEM_BOX_GAP by our narrower road (fewer per row).

    Boxes that already shared a spot in the template (moving boxes starting together) stay together.
    """
    kept = []
    for m in moved:
        if m["class"] != "BP_ItemBox_C" or m.get("delete"):
            continue
        p, f = np.array(m["location"]) / 100.0, np.array(m["from"]) / 100.0
        if any(np.linalg.norm(p - q) < ITEM_BOX_GAP and np.linalg.norm(f - g) > 0.5 for q, g in kept):
            m["delete"] = True
            m["location"], m["rotation"] = m["from"], m["from_rotation"]
        else:
            kept.append((p, f))
    return moved


# Gimmicks whose physical shape lives in the stage's Bullet collision rather than on the actor
# (jump-stand ramps and their triggers, blockers, final-lap road additions). Dash panels and
# rings have no collision chunks -- their effect comes from the actor.
COLLISION_CLASSES = {"BP_GuidedJumpStand_C", "BP_RedStarRing_C", "StaticMeshActor"}
MATCH_RADIUS = 8.0     # chunk centre to actor origin, metres
SMALL_CHUNK = 60.0     # gimmick chunks are at most this large


def _cw_rotation(rotator):
    m = rotator_to_matrix(*rotator)
    return CW_TO_UE.T @ m @ CW_TO_UE


def move_gimmick_collision(template_col, track_col, actors: list[dict], moved: list[dict]) -> int:
    """Append the template's gimmick collision chunks, rigidly moved with their actors."""
    new_by_name = {m["name"]: m for m in moved if not m.get("delete")}
    cands = [a for a in actors if a["class"] in COLLISION_CLASSES and a["name"] in new_by_name]
    if not cands:
        return 0
    origins = np.array([ue_to_cw(a["location"]) for a in cands])
    added = 0
    for ch in template_col.chunks:
        v = ch.vertices.astype(np.float64)
        ext = v.max(0) - v.min(0)
        if ext.max() > SMALL_CHUNK:
            continue
        centre = (v.max(0) + v.min(0)) / 2
        dist = np.linalg.norm(origins - centre, axis=1)
        j = int(np.argmin(dist))
        if dist[j] > MATCH_RADIUS:
            continue
        a, m = cands[j], new_by_name[cands[j]["name"]]
        r_old, r_new = _cw_rotation(a["rotation"]), _cw_rotation(m["rotation"])
        p_old, p_new = ue_to_cw(a["location"]), ue_to_cw(m["location"])
        moved_v = (v - p_old) @ (r_new @ r_old.T).T + p_new
        track_col.chunks.append(type(ch)(moved_v.astype(np.float32), list(ch.materials), list(ch.parts), dict(ch.header)))
        added += 1
    return added


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("actors", type=Path, help="tools/ue_dump_actors.py output")
    ap.add_argument("converted", type=Path, help="asr_to_cw output folder")
    ap.add_argument("--template", default="stg1016")
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--collision", action="store_true",
                    help="also move the template's gimmick collision chunks into the converted collision files")
    ap.add_argument("--levels", type=Path, help="legacy-extracted object levels (see pipeline.patch_object_levels)")
    ap.add_argument("--visual", type=Path, help="the track's OBJ (forest_to_obj; default: <converted>/../*.obj) "
                    "to rest boost pads on the visible surface")
    args = ap.parse_args()
    t_road = drivable_triangles(parse_collision((TEMPLATE_DIR / f"{args.template}_Lap_Collision.bin").read_bytes()))
    n_road = drivable_triangles(parse_collision((args.converted / "Lap_Collision.bin").read_bytes()))
    src = Route((TEMPLATE_DIR / f"{args.template}_Lap_Navigation.bin").read_bytes(), t_road,
                origin=START_SHIFTS.get(args.template, 0.0))
    dst = Route((args.converted / "Lap_Navigation.bin").read_bytes(), n_road)
    actors = json.loads(args.actors.read_text())
    if args.levels:
        from pipeline.patch_object_levels import read_level_actors
        stage_dir = f"/Game/01_Union/Map/Stage/{args.template}"
        for suffix, keep in EXTRA_LEVELS.items():
            level = f"{stage_dir}/SL_{args.template}{suffix}"
            base = args.levels / "UNION" / "Content" / level.replace("/Game/", "")
            rows = read_level_actors(Path(str(base) + ".umap").read_bytes(), Path(str(base) + ".uexp").read_bytes())
            actors[level] = [r | {"delete": not keep} for r in rows]
    obj = args.visual or next((o for o in sorted(args.converted.parent.glob("*.obj")) if ".prev" not in o.name), None)
    visual = None
    if obj:
        from pipeline.visual_road import load
        visual = load(obj, args.converted / "align.json")
        print(f"visible track: {len(visual)} triangles from {obj.name}")
    result = retarget(actors, src, dst, visual)
    args.out.write_text(json.dumps(result, indent=1))
    if args.collision:
        from cw.collision import write_collision
        for lap, suffix in (("Lap", "_Object_Game"), ("FinalLap", "_Object_Game_FinalLap")):
            level = next((k for k in actors if k.endswith(suffix)), None)
            track_path = args.converted / f"{lap}_Collision.track.bin"
            out_path = args.converted / f"{lap}_Collision.bin"
            if level is None or not out_path.exists():
                continue
            if not track_path.exists():
                track_path.write_bytes(out_path.read_bytes())  # keep the track-only version
            col = parse_collision(track_path.read_bytes())
            tmpl = parse_collision((TEMPLATE_DIR / f"{args.template}_{lap}_Collision.bin").read_bytes())
            n = move_gimmick_collision(tmpl, col, actors[level], result[level])
            out_path.write_bytes(write_collision(col))
            print(f"{lap} collision: added {n} gimmick chunks from {level.split('/')[-1]}")
    for level, rows in result.items():
        print(f"{level.split('/')[-1]}: {len(rows)} actors retargeted")
        for r in rows:
            if "Travel" in r["class"] or "Goal" in r["label"] or "Desid" in r["class"]:
                print(f"   {r['label'][:34]:34s} lap {r['lap_distance'][0]:7.1f} -> {r['lap_distance'][1]:7.1f}  "
                      f"UE {np.round(r['from']).astype(int)} -> {np.round(r['location']).astype(int)}")


if __name__ == "__main__":
    main()
