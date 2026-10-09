"""Draw the course map (race HUD minimap) of the new track in CrossWorlds' style.

    python -m pipeline.minimap out/samba_Hard --stage stg1016

The stage's course-map textures (UI/Race/MiniMap/Texture/CourseMapTexture/T_<stage>[_F]_Editor_*,
1024^2 BC7 with alpha, 11 mips) show the road from above inside the square DA_<stage> defines
(OrthoRate, Offset, Angle -90; ue_build.patch_course_map fits it to the new track). Projection, measured
against E-Stadium's own map (IoU 0.82 with its road collision): p = (UE XY - centre) / OrthoRate, rotated
by -90 degrees ((x, y) -> (y, -x)), pixel = (p + 0.5) * 1024.

Style copied from the stock maps: road white (241) at alpha 0.8, a soft dark drop shadow around it
(alpha up to 30/255 over ~20 px), lower roads darkened next to the roads crossing above them, and a
checkered finish-line bar at the goal (BP_GimmickGoalglow). Drawn at 2x and downsampled.
The textures are written over copies of the stock ones (loading_screen.replace_mips) to out/ui_cooked.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from cw.collision import parse_collision
from pipeline.asr_to_cw import drivable_triangles
from pipeline.loading_screen import replace_mips
from pipeline.ue_build import ENGINE, ROOT, course_map_square

MAP_DIR = "01_Union/UI/Race/MiniMap/Texture/CourseMapTexture"
OUT = ROOT / "out" / "ui_cooked" / "UNION" / "Content" / MAP_DIR
RES, SS = 1024, 4            # texture size, supersampling (smooth edges)
ROAD_RGB, ROAD_ALPHA = 241, 204
SHADOW_RGB, SHADOW_ALPHA, SHADOW_SIGMA = 25, 30, 8.0   # px at 1024
CROSS_GAP = 3.0              # m: a road this much higher than another is an overpass
CROSS_DARK, CROSS_SIGMA = 0.45, 4.0
PASS_GAP = 60.0              # m along the lap: closer surfaces are the same stretch of road


def project(ue_xy: np.ndarray, size: float, centre: np.ndarray) -> np.ndarray:
    p = (ue_xy - centre) / size
    p = np.stack([p[..., 1], -p[..., 0]], -1)
    return (p + 0.5) * RES * SS


def blur(a: np.ndarray, sigma: float) -> np.ndarray:
    """Float Gaussian approximation (three box passes per axis), sigma in 1024-px units."""
    sig = sigma * SS
    r = max(1, int(round((np.sqrt(12 * sig * sig / 3 + 1) - 1) / 2)))
    out = np.asarray(a, np.float64)
    for axis in (0, 1):
        for _ in range(3):
            pad = [(0, 0), (0, 0)]
            pad[axis] = (r + 1, r)
            c = np.cumsum(np.pad(out, pad, mode="edge"), axis=axis)
            hi = np.take(c, np.arange(2 * r + 1, c.shape[axis]), axis=axis)
            lo = np.take(c, np.arange(0, c.shape[axis] - 2 * r - 1), axis=axis)
            out = (hi - lo) / (2 * r + 1)
    return out.astype(np.float32)


SUBDIV = 6


def asr_road_edges(track_dir: Path, stage: str):
    """(left, right) road edges per ASR waypoint in CW space, in lap order."""
    from pipeline.asr_to_cw import AsrTrack
    from asr.sif import read_sif
    sif = ROOT / "extracted" / "asr" / "Resource" / "Tracks" / f"{track_dir.name.replace('samba_', 'Samba_')}.sif"
    from asr.navigation import parse_navigation as parse_asr_navigation
    data = read_sif(sif)
    track = AsrTrack(data)
    nav = parse_asr_navigation(data["TRAK"])
    wps = nav.waypoints
    start = next(w.index for w in wps if w.name == "ExtraStartLineWaypoint")
    # same order as AsrTrack.centre
    order, i = [], start
    while True:
        order.append(i)
        nxt = nav.links[wps[i].to_links[0]].to_wp
        if nxt == start:
            break
        i = nxt
    pos = np.array([wps[i].pos for i in order]); dirs = np.array([wps[i].dir for i in order])
    if np.mean(np.einsum("ij,ij->i", dirs[:-1], np.diff(pos, axis=0)) > 0) < 0.5:
        order = [order[0]] + order[1:][::-1]
    links = [nav.links[wps[i].to_links[0]] for i in order]
    a = json.loads((track_dir / f"cw_{stage}" / "align.json").read_text())
    rot, off = np.array(a["rotation"]), np.array(a["offset"])
    left = np.array([l.left for l in links], np.float64) @ rot.T + off
    right = np.array([l.right for l in links], np.float64) @ rot.T + off
    return left, right


def catmull(p: np.ndarray, k: int) -> np.ndarray:
    """Closed Catmull-Rom curve through p, k points per segment."""
    p0, p1, p2, p3 = np.roll(p, 1, 0), p, np.roll(p, -1, 0), np.roll(p, -2, 0)
    out = []
    for t in np.arange(k) / k:
        t2, t3 = t * t, t * t * t
        out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                          + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    return np.stack(out, 1).reshape(-1, 3)


def draw(track_dir: Path, stage: str) -> Image.Image:
    size, centre = course_map_square(track_dir, stage)
    road = drivable_triangles(parse_collision((track_dir / f"cw_{stage}" / "Lap_Collision.track.bin").read_bytes()))
    road = road.astype(np.float64)
    n = RES * SS
    # Drawn as a clean ribbon (like the stock maps), not from the collision triangles: steeply banked
    # and spiralling road overlaps itself in plan and left speckles. Edges = ASR's own road edges per
    # waypoint (navigation link left/right), mapped like the track (align.json), Catmull-Rom
    # subdivided; each segment is a quad plus a round join.
    l0, r0 = asr_road_edges(track_dir, stage)
    # A few single waypoints have odd edge data (one 24.7 m among ~20 m at lap 1711 m, one 16.8 m at
    # 3036 m): they drew a ball and a notch. Width is median-filtered and laid symmetrically about
    # the centre.
    c0, half = (l0 + r0) / 2, (l0 - r0) / 2
    w0 = np.linalg.norm(half, axis=1)
    idx = (np.arange(len(w0))[:, None] + np.arange(-3, 4)[None, :]) % len(w0)
    w1 = np.median(w0[idx], axis=1)
    odd = np.abs(w0 - w1) > 0.1 * w1  # their centre is off too (one edge pulled in): use the neighbours'
    c0 = np.where(odd[:, None], (np.roll(c0, 1, 0) + np.roll(c0, -1, 0)) / 2, c0)
    half = half / w0[:, None] * w1[:, None]
    left, right = catmull(c0 + half, SUBDIV), catmull(c0 - half, SUBDIV)
    pos = (left + right) / 2
    m = len(pos)
    to_px = lambda p: project(np.stack([p[..., 2] * 100, -p[..., 0] * 100], -1), size, centre)
    pa, pb, pc = to_px(left), to_px(right), to_px(pos)
    radius = np.linalg.norm(pa - pb, axis=1) / 2
    lap = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pos, axis=0), axis=1))]
    total_lap = float(lap[-1] + np.linalg.norm(pos[0] - pos[-1]))
    seg_h = (pos[:, 1] + np.roll(pos[:, 1], -1)) / 2
    hmap, lmap = Image.new("F", (n, n), -1e9), Image.new("F", (n, n), 1e9)
    smap_h, smap_l = Image.new("F", (n, n), 0.0), Image.new("F", (n, n), 0.0)
    dr, dl = ImageDraw.Draw(hmap), ImageDraw.Draw(lmap)
    dsh, dsl = ImageDraw.Draw(smap_h), ImageDraw.Draw(smap_l)

    def shapes(k):
        k2 = (k + 1) % m
        quad = [tuple(pa[k]), tuple(pa[k2]), tuple(pb[k2]), tuple(pb[k])]
        r = radius[k]
        disc = [pc[k][0] - r, pc[k][1] - r, pc[k][0] + r, pc[k][1] + r]
        return quad, disc

    order = np.argsort(seg_h)
    for k in order:
        quad, disc = shapes(k)
        for drw, val in ((dr, seg_h[k]), (dsh, lap[k])):
            drw.polygon(quad, fill=float(val))
            drw.ellipse(disc, fill=float(val))
    for k in order[::-1]:
        quad, disc = shapes(k)
        for drw, val in ((dl, seg_h[k]), (dsl, lap[k])):
            drw.polygon(quad, fill=float(val))
            drw.ellipse(disc, fill=float(val))
    h, low = np.asarray(hmap, np.float32), np.asarray(lmap, np.float32)
    sh, sl = np.asarray(smap_h, np.float32), np.asarray(smap_l, np.float32)
    mask = (h > -1e8).astype(np.float32)
    total = total_lap
    # Lower roads darkened next to roads passing above them (stock style: dark right at the upper
    # road's edge, fading out). The lap is cut into PASS_GAP-long pieces; each piece casts a blurred
    # shadow onto visible road that is CROSS_GAP lower and lap-far from it (so a road never shades
    # itself on a slope, and spiral turns shade the turn below).
    on = mask > 0
    dark = np.zeros_like(h)
    for b0 in np.arange(0.0, total, PASS_GAP):
        src = on & (((sh - b0) % total) < PASS_GAP)
        if not src.any():
            continue
        w = blur(src.astype(np.float32), CROSS_SIGMA)
        hb = blur(np.where(src, h, 0.0), CROSS_SIGMA) / np.maximum(w, 1e-4)
        far = np.abs((sh - (b0 + PASS_GAP / 2) + total / 2) % total - total / 2) > 1.5 * PASS_GAP
        recv = on & ~src & far & (h < hb - CROSS_GAP)
        dark = np.where(recv, np.maximum(dark, np.clip(w * 2, 0, 1)), dark)
    shade = 1.0 - CROSS_DARK * dark
    shadow = np.clip(blur(mask, SHADOW_SIGMA) * 2, 0, 1) * SHADOW_ALPHA / 255
    a_road = mask * ROAD_ALPHA / 255
    alpha = a_road + shadow * (1 - a_road)
    rgb_road = ROAD_RGB * shade
    rgb = np.where(alpha > 0, (rgb_road * a_road + SHADOW_RGB * shadow * (1 - a_road)) / np.maximum(alpha, 1e-6), 0)
    img = np.dstack([rgb, rgb, rgb, alpha * 255]).clip(0, 255).astype(np.uint8)
    out = Image.fromarray(img, "RGBA")
    finish_line(out, track_dir, size, centre)
    return out.resize((RES, RES), Image.LANCZOS)


def finish_line(img: Image.Image, track_dir: Path, size: float, centre: np.ndarray) -> None:
    """Checkered bar (2 rows) across the road at the goal actor, like the stock maps."""
    actors = json.loads(next((track_dir / "unreal").glob("*_actors_moved.json")).read_text())
    goal = next((a for rows in actors.values() for a in rows if a["class"] == "BP_GimmickGoalglow_C"
                 and not a.get("delete")), None)
    if goal is None:
        return

    from pipeline.retarget_actors import Route, ue_to_cw
    stage_dir = next(track_dir.glob("cw_*"))
    road = drivable_triangles(parse_collision((stage_dir / "Lap_Collision.track.bin").read_bytes())).astype(float)
    route = Route((stage_dir / "Lap_Navigation.bin").read_bytes(), road)
    s, _, _, _ = route.locate(ue_to_cw(goal["location"]))
    _, d, _, _ = route.at(s)                       # road direction (CW) at the goal
    pos = np.array(goal["location"][:2])
    fwd = np.array([d[2], -d[0]])                  # -> UE X, Y
    fwd /= np.linalg.norm(fwd)
    side = np.array([-fwd[1], fwd[0]])
    half_w, cell = 1300.0, 260.0     # cm: across the road, square size
    dr = ImageDraw.Draw(img)
    cols = int(2 * half_w / cell)
    for r in range(2):
        for c in range(cols):
            a = pos + side * (-half_w + c * cell) + fwd * (r - 1) * cell
            quad = [a, a + side * cell, a + side * cell + fwd * cell, a + fwd * cell]
            q = project(np.array(quad), size, centre)
            dr.polygon([tuple(v) for v in q], fill=(20, 20, 20, 255) if (r + c) % 2 else (250, 250, 250, 255))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("track_dir", type=Path)
    ap.add_argument("--stage", default="stg1016")
    args = ap.parse_args()
    img = draw(args.track_dir, args.stage)
    img.save(args.track_dir / "unreal" / "course_map.png")
    src_dir = ENGINE / "UNION" / "Content" / MAP_DIR
    OUT.mkdir(parents=True, exist_ok=True)
    for uexp in sorted(src_dir.glob(f"T_{args.stage}_*Editor_*.uexp")):
        (OUT / uexp.name).write_bytes(replace_mips(uexp.read_bytes(), img))
        shutil.copyfile(uexp.with_suffix(".uasset"), OUT / uexp.with_suffix(".uasset").name)
        print(f"wrote {OUT / uexp.name}")


if __name__ == "__main__":
    main()
