"""Convert an ASR track's gameplay data into CrossWorlds stage data payloads.

    python -m pipeline.asr_to_cw extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/cw --template stg1901

Both games are Y-up with matching road widths (ASR ~39 units, CW 34-45), so
geometry is copied 1:1 without mirroring. Collision is laid out like the
template stage's: a respawn shell chunk, a simplified road/wall layer
(0x02000000 flag), and the main chunk with per-surface road parts and walls,
reusing the template's 16-byte surface materials.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import struct
from pathlib import Path

import numpy as np

from asr.collision import parse_collision as parse_asr_collision
from asr.navigation import parse_navigation as parse_asr_navigation
from asr.sif import read_sif
from cw.collision import Collision, CollisionChunk, MeshPart, parse_collision, write_collision
from cw.compath import POINT_DTYPE, ComPath, Junction, Path as CPath, encode_routing, parse_compath, write_compath
from cw.navigation import LINK, Link, Marker, Navigation, Waypoint, parse_navigation, write_navigation

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "extracted" / "cw" / "blobs"

# Flag words of CW surface materials (first u32 of the 16-byte record).
# Road keeps flag bit 0x2 like E-Stadium's main road (0x3, id 0x2c): retail tracks put it on almost
# all steep/banked road (plain 0x1 road is 1.9% steeper than 45 deg, 0x3 road 19%, 0x103 52%), and
# without it CrossWorlds reset karts on Rocky Coaster's 45-68 deg banked spiral climb (in-game path log).
ROAD, WALL, RESPAWN = 0x00000003, 0x00400004, 0x00000020
LAYER2_ROAD, LAYER2_WALL = 0x02000001, 0x02000004


def template_materials(stage: str) -> dict[int, bytes]:
    """Most-used material record (by triangle count) for each flag word in the template stage."""
    col = parse_collision((TEMPLATE_DIR / f"{stage}_Lap_Collision.bin").read_bytes())
    counts: dict[bytes, int] = {}
    for ch in col.chunks:
        for p in ch.parts:
            m = bytes(ch.materials[p.material])
            counts[m] = counts.get(m, 0) + len(p.triangles)
    found: dict[int, bytes] = {}
    for m, _ in sorted(counts.items(), key=lambda kv: -kv[1]):
        found.setdefault(struct.unpack_from("<I", m, 0)[0], m)
    missing = {ROAD, WALL, RESPAWN, LAYER2_ROAD, LAYER2_WALL} - found.keys()
    if missing:
        raise ValueError(f"template {stage} lacks materials {[hex(m) for m in missing]}")
    return found


def _chunk(vertices: np.ndarray, groups: list[tuple[np.ndarray, bytes]]) -> CollisionChunk:
    """One chunk holding the given triangle groups, with vertices compacted."""
    used = np.unique(np.concatenate([t.ravel() for t, _ in groups]))
    remap = np.full(len(vertices), -1, np.int64)
    remap[used] = np.arange(len(used))
    materials, parts = [], []
    for tris, mat in groups:
        if mat not in materials:
            materials.append(mat)
        parts.append(MeshPart(remap[tris].astype(np.uint32), materials.index(mat)))
    return CollisionChunk(vertices[used].astype(np.float32), materials, parts, {"version": 1, "unknown": 2})


def convert_collision(track: "AsrTrack", template: str) -> Collision:
    mats = template_materials(template)
    verts = track.vertices.astype(np.float32)
    tris = track.triangles
    names = track.surface_names

    road_kinds = sorted({n for n in names if n.startswith("track_")})
    road = np.isin(names, road_kinds)
    wall = names == "barrier_woodsplinters"
    respawn = names == "reset"
    unknown = ~(road | wall | respawn)
    if unknown.any():
        raise ValueError(f"unmapped ASR surfaces: {sorted(set(names[unknown]))}")

    # ASR leaves many edges open and catches karts with reset volumes; CrossWorlds karts just fly
    # off (e.g. after Rocky Coaster's mid-lap bumps), so close every open edge with a barrier.
    bverts, btris = edge_barriers(track, verts[tris[road]].astype(np.float64), verts[tris[wall]].astype(np.float64))
    if len(btris):
        verts = np.vstack([verts, bverts.astype(np.float32)])
        tris = np.vstack([tris, btris + len(track.vertices)])
        wall = np.concatenate([wall, np.ones(len(btris), bool)])
        road = np.concatenate([road, np.zeros(len(btris), bool)])
        respawn = np.concatenate([respawn, np.zeros(len(btris), bool)])
        names = np.concatenate([names, np.full(len(btris), "barrier_generated")])
        print(f"collision: added {len(btris)} barrier triangles along open road edges")

    chunks = [
        _chunk(verts, [(tris[respawn], mats[RESPAWN])]),
        _chunk(verts, [(tris[wall], mats[LAYER2_WALL])]),
        _chunk(verts, [(tris[road], mats[LAYER2_ROAD])]),
        # Main chunk: one road part per ASR surface (keeps them separable for
        # later per-surface sound/effect tuning), then walls.
        _chunk(verts, [(tris[names == k], mats[ROAD]) for k in road_kinds] + [(tris[wall], mats[WALL])]),
    ]
    return Collision(version=1, hash=0, chunks=chunks)


BARRIER_SPACING = 2.0   # m between barrier posts along the route
BARRIER_HEIGHT = 6.0    # m above the road edge
BARRIER_DEPTH = 1.0     # m below it (shallow, so it cannot reach a road passing underneath)
BARRIER_CLEARANCE = 0.5  # m outside the drivable edge


def edge_barriers(track: "AsrTrack", road: np.ndarray, walls: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vertical wall strips just outside the drivable edge wherever ASR has no wall there."""
    pos, tan, up, _, _ = _resample(track.centre, track.ups, BARRIER_SPACING)
    wc = walls.mean(1) if len(walls) else np.zeros((0, 3))
    cell = 4.0
    grid: dict[tuple, list[int]] = {}
    for i, c in enumerate(np.floor(wc / cell).astype(int)):
        grid.setdefault(tuple(c), []).append(i)

    def has_wall(p):
        c = np.floor(p / cell).astype(int)
        idx = [i for dx in (-1, 0, 1) for dy in (-1, 0, 1) for dz in (-1, 0, 1)
               for i in grid.get((c[0] + dx, c[1] + dy, c[2] + dz), ())]
        return bool(idx) and np.min(np.linalg.norm(wc[idx] - p, axis=1)) < 3.0

    verts, tris = [], []
    for sign in (1, -1):
        prev = None
        for k in range(len(pos)):
            lat = np.cross(up[k], tan[k])
            lat /= np.linalg.norm(lat)
            ext = road_extent(road, pos[k], lat, up[k], reach=40.0)[0 if sign > 0 else 1]
            base = pos[k] + lat * sign * (ext + BARRIER_CLEARANCE)
            ok = ext > 0.0 and not has_wall(base)
            if ok and prev is not None and np.linalg.norm(base - prev[0]) < 3 * BARRIER_SPACING:
                n = len(verts)
                verts += [prev[0] - prev[1] * BARRIER_DEPTH, prev[0] + prev[1] * BARRIER_HEIGHT,
                          base - up[k] * BARRIER_DEPTH, base + up[k] * BARRIER_HEIGHT]
                tris += [[n, n + 1, n + 2], [n + 1, n + 3, n + 2]]
            prev = (base, up[k]) if ok else None
    return np.array(verts).reshape(-1, 3), np.array(tris, np.int64).reshape(-1, 3)


# --- Shared path helpers ---------------------------------------------------------

def _resample(points: np.ndarray, ups: np.ndarray, spacing: float, closed: bool = True):
    """Arc-length resample a polyline; returns (pos, tangent, up, cumulative distance)."""
    pts = np.vstack([points, points[:1]]) if closed else points
    upv = np.vstack([ups, ups[:1]]) if closed else ups
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    s = np.concatenate([[0], np.cumsum(seg)])
    total = s[-1]
    n = max(int(round(total / spacing)), 4)
    t = np.linspace(0, total, n, endpoint=not closed)
    pos = np.stack([np.interp(t, s, pts[:, i]) for i in range(3)], 1)
    up = np.stack([np.interp(t, s, upv[:, i]) for i in range(3)], 1)
    up /= np.linalg.norm(up, axis=1, keepdims=True)
    nxt = np.roll(pos, -1, 0) if closed else np.vstack([pos[1:], 2 * pos[-1] - pos[-2]])
    tangent = nxt - pos
    tangent /= np.linalg.norm(tangent, axis=1, keepdims=True)
    # Make up orthogonal to the tangent so frames are well formed.
    up -= tangent * np.einsum("ij,ij->i", up, tangent)[:, None]
    up /= np.linalg.norm(up, axis=1, keepdims=True)
    return pos, tangent, up, t, total


def _simplify(points: np.ndarray, tol: float) -> np.ndarray:
    """Douglas-Peucker on a polyline; returns kept indices (endpoints always kept)."""
    keep = np.zeros(len(points), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        ab = points[b] - points[a]
        n = np.linalg.norm(ab)
        rel = points[a + 1:b] - points[a]
        if n < 1e-9:
            d = np.linalg.norm(rel, axis=1)
        else:
            d = np.linalg.norm(np.cross(rel, ab / n), axis=1)
        k = int(np.argmax(d))
        if d[k] > tol:
            m = a + 1 + k
            keep[m] = True
            stack += [(a, m), (m, b)]
    return np.flatnonzero(keep)


class AsrTrack:
    """An ASR track's collision and route, optionally moved by a rigid transform.

    The transform is a rotation about +Y followed by a translation, used to put
    the ASR start line exactly on the template stage's start line so objects
    placed in the template's levels (goal glow, line guides) line up.
    """

    def __init__(self, sif: dict[str, bytes]):
        col = parse_asr_collision(sif["COLI"])
        self.vertices = col.vertices.astype(np.float64)
        self.triangles = col.triangles.astype(np.int64)
        self.surface_names = np.array([col.surface_name(h) for h in col.surfaces])

        nav = parse_asr_navigation(sif["TRAK"])
        wps = nav.waypoints
        start = next(w.index for w in wps if w.name == "ExtraStartLineWaypoint")
        order, i = [], start
        while True:
            order.append(i)
            nxt = nav.links[wps[i].to_links[0]].to_wp
            if nxt == start:
                break
            i = nxt
        if len(order) != len(wps):
            raise ValueError("ASR route is not a single loop; branching tracks need path splitting")
        # Following ToLinks walks the lap backwards (track_dist decreases, waypoint dir points the
        # other way), so check against the waypoints' own direction and reverse if needed.
        pos = np.array([wps[i].pos for i in order])
        dirs = np.array([wps[i].dir for i in order])
        if np.mean(np.einsum("ij,ij->i", dirs[:-1], np.diff(pos, axis=0)) > 0) < 0.5:
            order = [order[0]] + order[1:][::-1]
        # Centre between the link's left/right road edges; racing-line segments indexed by link start.
        links = [nav.links[wps[i].to_links[0]] for i in order]
        self.centre = np.array([(np.array(l.left) + np.array(l.right)) / 2 for l in links])
        self.ups = np.array([wps[i].up for i in order], np.float64)
        by_wp = {nav.links[sg.link].from_wp: sg for sg in nav.racing_lines[0].segments}
        self.racing = np.array([by_wp[i].racing_line for i in order], np.float64)
        self.safe = np.array([by_wp[i].safe_racing_line for i in order], np.float64)
        # The start waypoint marks the painted start/finish line (leading edge of the start-area
        # markings); the link-edge midpoint used for `centre` sits ~10 m before it.
        self.start_point = np.array(wps[order[0]].pos, np.float64)
        # Composite transform applied so far: cw = rotation @ asr + offset.
        self.rotation = np.eye(3)
        self.offset = np.zeros(3)

    @property
    def start_frame(self):
        d = self.centre[1] - self.centre[0]
        return self.start_point, d / np.linalg.norm(d)

    def transform(self, yaw: float, offset: np.ndarray) -> None:
        c, s_ = np.cos(yaw), np.sin(yaw)
        rot = np.array([[c, 0, s_], [0, 1, 0], [-s_, 0, c]])
        for name in ("vertices", "centre", "racing", "safe"):
            setattr(self, name, getattr(self, name) @ rot.T + offset)
        self.start_point = rot @ self.start_point + offset
        self.ups = self.ups @ rot.T
        self.rotation = rot @ self.rotation
        self.offset = rot @ self.offset + offset

    def scale(self, factor: float) -> None:
        """Uniform scale about the origin (ASR units are larger than CrossWorlds metres)."""
        for name in ("vertices", "centre", "racing", "safe"):
            setattr(self, name, getattr(self, name) * factor)
        self.start_point = self.start_point * factor
        self.rotation = self.rotation * factor
        self.offset = self.offset * factor

    def align_start_to(self, pos: np.ndarray, direction: np.ndarray) -> None:
        """Rotate about Y and translate so the start line and travel direction match."""
        p0, d0 = self.start_frame
        yaw = np.arctan2(direction[0], direction[2]) - np.arctan2(d0[0], d0[2])
        self.transform(yaw, np.zeros(3))
        p1, _ = self.start_frame
        self.transform(0.0, pos - p1)

    def shift_start(self, distance: float) -> None:
        """Move the loop's first point `distance` metres back along the route (before the line).

        CW stages start their waypoint loop (travel index 0) some way before the finish
        line -- 119 m on E-Stadium -- and place grid/goal/travel-ring markers relative to it.
        """
        seg = np.linalg.norm(np.diff(np.vstack([self.centre, self.centre[:1]]), axis=0), axis=1)
        s = np.concatenate([[0], np.cumsum(seg)])
        # Measure from the painted line (start_point), which lies ~10 m past centre[0].
        tangent = (self.centre[1] - self.centre[0]) / seg[0]
        line_s = float((self.start_point - self.centre[0]) @ tangent)
        target = (line_s - distance) % s[-1]
        # ASR waypoints are ~19 m apart; resample all parallel arrays to ~1 m so the new start
        # lands within a metre of the target instead of up to half a waypoint away.
        n = len(self.centre)
        fine = np.linspace(0.0, s[-1], int(np.ceil(s[-1])), endpoint=False)
        fine = np.roll(fine, -int(np.argmin(np.abs(fine - target))))
        idx = np.clip(np.searchsorted(s, fine, side="right") - 1, 0, n - 1)
        frac = ((fine - s[idx]) / seg[idx])[:, None]
        for name in ("centre", "ups", "racing", "safe"):
            arr = getattr(self, name)
            setattr(self, name, arr[idx] + (arr[(idx + 1) % n] - arr[idx]) * frac)
        self.ups /= np.linalg.norm(self.ups, axis=1, keepdims=True)

    def advance_line(self, distance: float) -> None:
        """Move the start/finish line `distance` metres forward along the route."""
        if distance <= 0:
            return
        self.shift_start(-distance)
        self.start_point = self.centre[0].copy()

    def straight_shift(self, length: float, behind: float = 0.0, max_turn: float = 3.0,
                       max_pitch: float = 2.0, search: float = 250.0) -> float:
        """Smallest forward move of the line so the `length` metres ending `behind` metres before it
        are straight and flat.

        CrossWorlds lays its starting grid out in straight rows going back from the navigation loop's
        first waypoint (travel index 0, `behind` = the finish offset before the line): measured in game,
        3 rows of 4 karts, rows 24.8 m apart, ~70 m deep. On a bend the rear slots end up off the road.
        """
        seg = np.linalg.norm(np.diff(np.vstack([self.centre, self.centre[:1]]), axis=0), axis=1)
        s = np.concatenate([[0], np.cumsum(seg)])
        total = s[-1]
        tangent = (self.centre[1] - self.centre[0]) / seg[0]
        line_s = float((self.start_point - self.centre[0]) @ tangent)
        nxt = np.roll(self.centre, -1, axis=0) - self.centre
        d = nxt / np.linalg.norm(nxt, axis=1, keepdims=True)
        heading = np.degrees(np.arctan2(d[:, 0], d[:, 2]))
        pitch = np.degrees(np.arcsin(np.clip(d[:, 1], -1, 1)))
        for shift in np.arange(0.0, search, 1.0):
            end = line_s + shift - behind
            win = (s[:-1] - (end - length)) % total <= length
            ref = heading[np.argmin(np.abs((s[:-1] - end + total / 2) % total - total / 2))]
            turn = np.abs((heading[win] - ref + 180) % 360 - 180)
            if win.any() and turn.max() <= max_turn and np.abs(pitch[win]).max() <= max_pitch:
                return float(shift)
        raise ValueError(f"no straight {length:.0f} m stretch within {search:.0f} m of the start line")

    @property
    def road_triangles(self) -> np.ndarray:
        road = np.char.startswith(self.surface_names.astype(str), "track_")
        return self.vertices[self.triangles[road]]


def main_route(nav: Navigation) -> list[int]:
    """Indices of the main loop's waypoints in travel order.

    +0xE8 is the waypoint's group and +0xEC its travel index within the group;
    group 0 is the main loop and its index 0 is the start line (the grid
    markers 38/39 sit just behind it).
    """
    main = [i for i, w in enumerate(nav.waypoints) if struct.unpack_from("<I", w.raw, 0xE8)[0] == 0]
    return sorted(main, key=lambda i: struct.unpack_from("<I", nav.waypoints[i].raw, 0xEC)[0])


def template_start(template_nav: bytes, finish_offset: float = 0.0):
    """Position and travel direction on the template's main route `finish_offset` m past travel index 0."""
    nav = parse_navigation(template_nav)
    order = main_route(nav)
    pos = np.array([nav.waypoints[i].pos for i in order], np.float64)
    s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pos, axis=0), axis=1))])
    k = int(np.clip(np.searchsorted(s, finish_offset) - 1, 0, len(pos) - 2))
    t = (finish_offset - s[k]) / max(s[k + 1] - s[k], 1e-6)
    p = pos[k] + (pos[k + 1] - pos[k]) * t
    d = nav.waypoints[order[k]].dir.astype(np.float64)
    return p, d


# Lap distance from a stage's travel-index-0 waypoint to its finish line (goal glow actor),
# measured from the stage's object level. Unknown stages fall back to 0.
FINISH_OFFSETS = {"stg1016": 119.1}

# Where our travel index 0 goes, in metres past the template's own index 0. E-Stadium puts its finish
# line and travel ring ~116-124 m past index 0, so the travel sequence straddles the lap wrap; on our
# track race progress froze at index 0 and the ring never fired. stg1501, verified working in game,
# has the same markers 113.6 m earlier (finish 4 m, ring 10 m past index 0; the teleport happens
# before index 0). Shifting the template origin by that much gives the stg1501 layout.
START_SHIFTS = {"stg1016": 0.0}   # 113.6 = stg1501-style layout (portal fires before index 0); 0 = E-Stadium's own
WAYPOINT_EDGES = True        # fill the per-waypoint road corridor from our collision (test 2)
NAV_DONOR = "stg1018"        # test 3: transplant this retail stage's navigation structure (None = our own)
RANGE_REFERENCE = "stg1016"  # copy this stage's waypoint ranges (None: template travel-0 waypoint's, all 0;
                         # off = keep the template travel-0 waypoint's ranges, as in the build that
                         # first got through the travel ring)
RANGE_WINDOW = (-300.0, 300.0)        # metres around travel index 0 copied by absolute distance;
                                      # elsewhere by lap fraction


def snap_to_road(points: np.ndarray, ups: np.ndarray, road: np.ndarray, reach: float = 8.0) -> np.ndarray:
    """Move points along their up vector onto the nearest road surface (CW stores them on it)."""
    centres = road.mean(1)
    out = points.copy()
    for i, (o, u) in enumerate(zip(points, ups)):
        near = road[np.linalg.norm(centres - o, axis=1) < 40]
        if not len(near):
            continue
        v0, e1, e2 = near[:, 0], near[:, 1] - near[:, 0], near[:, 2] - near[:, 0]
        p = np.cross(u, e2)
        det = np.einsum("ij,ij->i", e1, p)
        ok = np.abs(det) > 1e-9
        inv = np.where(ok, 1 / np.where(ok, det, 1), 0)
        s = o - v0
        a = np.einsum("ij,ij->i", s, p) * inv
        q = np.cross(s, e1)
        b = (q @ u) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv      # signed distance along +up
        hit = ok & (a >= 0) & (b >= 0) & (a + b <= 1) & (np.abs(t) < reach)
        if hit.any():
            out[i] = o + u * t[hit][np.argmin(np.abs(t[hit]))]
    return out


def road_extent(road: np.ndarray, pos: np.ndarray, lat: np.ndarray, up: np.ndarray,
                reach: float = 80.0, step: float = 0.5, depth: float = 10.0) -> tuple[float, float]:
    """Contiguous drivable width left (+lat) and right (-lat) of pos, by casting rays down.

    Rays start 5 above pos and accept road down to `depth` below pos; a small depth keeps a road
    deck running underneath (Rocky Coaster's finish stretch) from counting as width."""
    near = road[np.linalg.norm(road.mean(1) - pos, axis=1) < reach + 20]
    if not len(near):
        return 0.0, 0.0
    v0, e1, e2 = near[:, 0], near[:, 1] - near[:, 0], near[:, 2] - near[:, 0]
    d = -up
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    ok = np.abs(det) > 1e-9
    inv = np.where(ok, 1 / np.where(ok, det, 1), 0)

    def hit(o):
        sv = o - v0
        a = np.einsum("ij,ij->i", sv, p) * inv
        q = np.cross(sv, e1)
        b = (q @ d) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv
        return bool(np.any(ok & (a >= 0) & (b >= 0) & (a + b <= 1) & (t > 0) & (t < 5 + depth)))

    extents = []
    for sign in (1, -1):
        w = 0.0
        while w < reach and hit(pos + lat * sign * (w + step) + up * 5):
            w += step
        extents.append(w)
    return extents[0], extents[1]


def drivable_triangles(col: Collision) -> np.ndarray:
    """Road triangles of a CW collision (drivable flag, excluding the simplified layer and walls)."""
    tris = []
    for ch in col.chunks:
        for part in ch.parts:
            flags = struct.unpack_from("<I", ch.materials[part.material], 0)[0]
            if flags & 1 and not flags & 0x02000004:
                tris.append(ch.vertices[part.triangles.astype(np.int64)])
    return np.concatenate(tris).astype(np.float64)


# --- Navigation ------------------------------------------------------------------

def _frame(d, up):
    lat = np.cross(up, d)
    return lat / np.linalg.norm(lat)


# ASR units are bigger than CrossWorlds metres: at 1:1 Rocky Coaster's road was 40 m wide and its lap
# 6.6 km (retail CW: median 29.5 m and 2.9 km), and karts looked half size next to ASR's.
# 0.5 gives a 20 m road and a 3.3 km lap that fits E-Stadium's footprint.
DEFAULT_SCALE = 0.5
GRID_MARKER_TYPE = 2            # nav marker the game lays the starting grid back from
GRID_MARKER_BEFORE_START = None  # metres before travel index 0 (experiment: 2.0 broke the race-start
                                 # crossing together with other changes; None = template spacing)
GRID_LENGTH = 75.0  # straight road behind travel index 0 for the 12-kart grid (measured ~70 m deep)
MARKER_FIXED_RANGE = 300.0  # units from the start/finish line where markers keep absolute distance
MARKER_MAX_ROAD_FRACTION = 0.85  # keep retargeted markers inside the road edge


def convert_navigation(track: AsrTrack, template_bytes: bytes, template_road: np.ndarray,
                       spacing: float = 4.8, start_shift: float = 0.0) -> Navigation:
    """Single-group waypoint loop (like stg1901) with template markers retargeted.

    Markers move by distance along the lap (absolute near the start/finish line, scaled
    elsewhere) and keep their sideways position as a fraction of the road width, so
    markers on a wide template road (e.g. travel-ring portals) stay on our narrower road.
    """
    tmpl = parse_navigation(template_bytes)
    our_road = track.road_triangles
    pos, tan, up, s_new, total = _resample(track.centre, track.ups, spacing)
    pos = snap_to_road(pos, up, track.road_triangles)
    n = len(pos)

    # Template travel order: CW stores waypoints in reverse, +0xEC is the travel index.
    t_order = main_route(tmpl)
    t_pos = np.array([tmpl.waypoints[i].pos for i in t_order])
    t_s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(t_pos, axis=0), axis=1))])
    t_total = t_s[-1] + np.linalg.norm(t_pos[0] - t_pos[-1])

    base = bytearray(tmpl.waypoints[t_order[0]].raw)
    for off in (0x80, 0x90, 0xA0, 0xB0, 0xC0):          # optional lateral limits: leave unset
        base[off:off + 16] = bytes(16)
    base[0xD0:0xE8] = bytes(0x18)
    # +60..+6F: four per-waypoint ranges. They decide whether race progress gets past travel index 0:
    # all zero (stg1901, a lapless course) -> progress froze after returning through the travel ring;
    # all 0/200/0/200 -> it froze even at the race start (in-game logs). Working tracks use a pattern
    # around index 0 (stg1501: zero from ~300 m before it, 0/50/50/50 from it on). Our start/finish
    # layout copies stg1501's (START_SHIFTS), so near index 0 copy its ranges by distance; elsewhere
    # use the template's most common value.
    default_ranges = Counter(bytes(tmpl.waypoints[i].raw[0x60:0x70]) for i in t_order).most_common(1)[0][0]
    if RANGE_REFERENCE is None:
        default_ranges = bytes(base[0x60:0x70])
    ref = parse_navigation((TEMPLATE_DIR / f"{RANGE_REFERENCE or 'stg1501_Extnd09'}_Lap_Navigation.bin").read_bytes())
    r_order = main_route(ref)
    r_pos = np.array([ref.waypoints[i].pos for i in r_order])
    r_s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(r_pos, axis=0), axis=1))])
    r_total = r_s[-1] + np.linalg.norm(r_pos[0] - r_pos[-1])
    r_d = (r_s + r_total / 2) % r_total - r_total / 2          # signed distance from index 0

    def ranges_at(sv: float) -> bytes:
        d = (sv + total / 2) % total - total / 2
        if RANGE_REFERENCE is None:
            return default_ranges
        if not RANGE_WINDOW[0] <= d <= RANGE_WINDOW[1]:
            d = d * r_total / total                    # proportional along the lap
        # the reference waypoint whose link we would be on: the last one at or before d
        before = np.where(r_d <= d + 1e-6, r_d, -np.inf)
        return bytes(ref.waypoints[r_order[int(np.argmax(before))]].raw[0x60:0x70])

    waypoints, links = [], []
    for i in range(n):
        k = n - 1 - i                                   # travel index of stored waypoint i
        raw = bytearray(base)
        struct.pack_into("<3fI3fI3fI", raw, 0, *pos[k], 0, *tan[k], 0, *up[k], 0)
        struct.pack_into("<II", raw, 0xE8, 0, k)
        raw[0x60:0x70] = ranges_at(s_new[k])
        if WAYPOINT_EDGES:
            # Road corridor (decoded from retail): +80/+90 left edge point, +A0/+B0 right edge point,
            # +C0 centre, +D4/+D8 -left width, +DC/+E0 right width. Retail sets it on 20-60% of
            # waypoints; ours had none anywhere (zero-width corridor).
            lat = np.cross(up[k], tan[k])
            lat /= np.linalg.norm(lat)
            wl, wr = road_extent(our_road, pos[k], lat, up[k], reach=60.0)
            if wl > 0 and wr > 0:
                left, right = pos[k] + lat * wl, pos[k] - lat * wr
                struct.pack_into("<3fI3fI3fI3fI3fI", raw, 0x80, *left, 0, *left, 0, *right, 0, *right, 0, *pos[k], 0)
                struct.pack_into("<6f", raw, 0xD0, 0.0, -wl, -wl, wr, wr, 0.0)
        waypoints.append(Waypoint(raw, [], [], -1))
    # Section structure: every retail race track groups waypoints into sections, each waypoint
    # pointing at its section's head (head itself: -1), and a section always spans the lap line
    # (E-Stadium: head 70 waypoints after travel index 0). stg1901 -- a lapless course -- has none,
    # and with none the game never wrapped race progress past travel index 0 (in-game log:
    # ElapsedRatio froze there, so no lap after the first ever counted and the travel ring never
    # fired). A single loop is one section; put its head as far past index 0 as the template's.
    t_head = tmpl.waypoints[t_order[0]].group
    head_s = t_s[t_order.index(t_head)] if t_head in t_order else 170.0
    k_head = int(np.argmin(np.abs(s_new[:n] - head_s)))
    head = n - 1 - k_head                               # stored index of the head
    for i, w in enumerate(waypoints):
        w.group = -1 if i == head else head
    for i in range(n):
        j = (i - 1) % n                                 # next waypoint along travel
        d = waypoints[j].pos - waypoints[i].pos
        length = float(np.linalg.norm(d))
        lraw = bytearray(LINK)
        struct.pack_into("<3f", lraw, 0, *(d / length))
        struct.pack_into("<f", lraw, 0x40, length)
        links.append(Link(lraw, i, j))
        waypoints[i].from_links = [i]
        waypoints[j].to_links = [i]

    def frame_at(sv, ss, P, T, U):
        k = int(np.clip(np.searchsorted(ss, sv) - 1, 0, len(P) - 1))
        return P[k] + T[k] * (sv - ss[k]), T[k], U[k]

    markers = []
    t_dir = np.array([tmpl.waypoints[i].dir for i in t_order])
    t_up = np.array([tmpl.waypoints[i].up for i in t_order])
    for m in tmpl.markers:
        mpos = np.array(struct.unpack_from("<3f", m.raw, 0))
        # Closest route segment, not closest waypoint: waypoints are tens of metres apart, and at
        # E-Stadium's crossover the travel-ring decide marker (type 12, on the upper road) is
        # closer to a waypoint on the road below -- which put it 1.5 km early on our lap and
        # broke the travel-ring sequence (rings never came down, no teleport).
        seg = np.roll(t_pos, -1, axis=0) - t_pos
        tt = np.clip(np.einsum("ij,ij->i", mpos - t_pos, seg) / np.einsum("ij,ij->i", seg, seg), 0.0, 1.0)
        k = int(np.argmin(np.linalg.norm(t_pos + seg * tt[:, None] - mpos, axis=1)))
        lat, d0 = _frame(t_dir[k], t_up[k]), t_dir[k]
        rel = mpos - t_pos[k]
        local = np.array([rel @ lat, rel @ t_up[k], rel @ d0])
        sv = t_s[k] + local[2] - start_shift
        # Near the start/finish keep the absolute distance (grid, goal); elsewhere scale with lap length.
        sv %= t_total
        if sv <= MARKER_FIXED_RANGE:
            sv_new = sv
        elif t_total - sv <= MARKER_FIXED_RANGE:
            sv_new = total - (t_total - sv)
        else:
            sv_new = sv * total / t_total
        if GRID_MARKER_BEFORE_START is not None and m.type == GRID_MARKER_TYPE:
            # The starting grid hangs ~70 m back from this marker in straight rows (measured in game);
            # keep it just before travel index 0 so the grid sits on the straight that
            # AsrTrack.straight_shift guaranteed behind index 0.
            sv_new = total - GRID_MARKER_BEFORE_START
        p, d1, u1 = frame_at(sv_new % total, s_new, pos, tan, up)
        lat1 = _frame(d1, u1)
        # Sideways: same fraction of the road's half-width on that side, kept on the road.
        side = 0 if local[0] >= 0 else 1
        t_ext = road_extent(template_road, t_pos[k], lat, t_up[k])[side]
        n_ext = road_extent(our_road, p, lat1, u1)[side]
        frac = abs(local[0]) / t_ext if t_ext > 1.0 else 0.0
        lateral = np.sign(local[0]) * min(frac, MARKER_MAX_ROAD_FRACTION) * n_ext
        raw = bytearray(m.raw)
        struct.pack_into("<3f", raw, 0, *(p + lat1 * lateral + u1 * local[1]))
        for off in (0x10, 0x20):                        # rotate dir/up into the new frame
            v = np.array(struct.unpack_from("<3f", m.raw, off))
            lv = np.array([v @ lat, v @ t_up[k], v @ d0])
            struct.pack_into("<3f", raw, off, *(lat1 * lv[0] + u1 * lv[1] + d1 * lv[2]))
        markers.append(Marker(bytes(raw)))

    if NAV_DONOR:
        return transplant_navigation(NAV_DONOR, total, s_new, pos, tan, up, our_road, markers)
    return Navigation(tmpl.version, 1, float(sum(l.length for l in links)), waypoints, links, markers)


def transplant_navigation(donor: str, total: float, s_new, pos, tan, up, our_road, markers) -> Navigation:
    """A retail stage's navigation with every structural byte kept (waypoint/link counts and
    topology, sections, path groups, flags, ranges, travel indices, header) and only the geometry
    moved onto our route: positions/directions by lap fraction, road corridors from our road,
    link directions and lengths recomputed. Markers are ours.
    """
    nav = parse_navigation((TEMPLATE_DIR / f"{donor}_Lap_Navigation.bin").read_bytes())
    order = main_route(nav)
    d_pos = np.array([w.pos for w in nav.waypoints], np.float64)
    m_pos = d_pos[order]
    m_s = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(m_pos, axis=0), axis=1))])
    d_total = m_s[-1] + np.linalg.norm(m_pos[0] - m_pos[-1])
    seg = np.roll(m_pos, -1, axis=0) - m_pos
    seg_len2 = np.maximum(np.einsum("ij,ij->i", seg, seg), 1e-12)

    def donor_s(p):
        tt = np.clip(np.einsum("ij,ij->i", p - m_pos, seg) / seg_len2, 0, 1)
        k = int(np.argmin(np.linalg.norm(m_pos + seg * tt[:, None] - p, axis=1)))
        return m_s[k] + tt[k] * np.sqrt(seg_len2[k])

    def frame(sv):
        k = int(np.clip(np.searchsorted(s_new, sv) - 1, 0, len(pos) - 1))
        return pos[k] + tan[k] * (sv - s_new[k]), tan[k], up[k]

    main_idx = {w: t for t, w in enumerate(order)}
    waypoints = []
    for i, w in enumerate(nav.waypoints):
        sv = m_s[main_idx[i]] if i in main_idx else donor_s(d_pos[i])
        p, d, u = frame((sv * total / d_total) % total)
        raw = bytearray(w.raw)
        struct.pack_into("<3fI3fI3fI", raw, 0, *p, 0, *d, 0, *u, 0)
        if any(w.raw[0x80:0xE8]):
            lat = np.cross(u, d)
            lat /= np.linalg.norm(lat)
            wl, wr = road_extent(our_road, p, lat, u, reach=60.0)
            wl, wr = max(wl, 0.5), max(wr, 0.5)
            left, right = p + lat * wl, p - lat * wr
            struct.pack_into("<3fI3fI3fI3fI3fI", raw, 0x80, *left, 0, *left, 0, *right, 0, *right, 0, *p, 0)
            struct.pack_into("<6f", raw, 0xD0, 0.0, -wl, -wl, wr, wr, 0.0)
        waypoints.append(Waypoint(raw, list(w.to_links), list(w.from_links), w.group))
    links = []
    for l in nav.links:
        dvec = waypoints[l.to_wp].pos - waypoints[l.from_wp].pos
        length = float(np.linalg.norm(dvec))
        if length < 0.05:
            dvec, length = tan[0], 0.05
        lraw = bytearray(l.raw)
        struct.pack_into("<3f", lraw, 0, *(dvec / np.linalg.norm(dvec)))
        struct.pack_into("<f", lraw, 0x40, length)
        links.append(Link(lraw, l.from_wp, l.to_wp))
    main_len = 0.0
    for t in range(len(order)):
        a, b = order[t], order[(t + 1) % len(order)]
        main_len += float(np.linalg.norm(waypoints[b].pos - waypoints[a].pos))
    return Navigation(nav.version, nav.unknown_count, main_len, waypoints, links, markers)


# --- AI paths ----------------------------------------------------------------------

# Retail stages: MAIN runs a full lap plus ~120-170 m past the line; GOAL covers the final
# ~450-900 m and ends ~170 m past the line, where karts head into the travel ring.
MAIN_OVERLAP = 140.0
GOAL_BEFORE = 600.0
GOAL_AFTER = 170.0


def _open_line(loop: np.ndarray, ups: np.ndarray, road: np.ndarray, s0: float, s1: float, tol: float = 0.25):
    """Simplified polyline along a closed loop from distance s0 to s1 (may wrap past the start)."""
    pos, _, up, _, _ = _resample(loop, ups, 1.0)
    idx = np.arange(int(np.floor(s0)), int(np.ceil(s1)) + 1) % len(pos)
    p, u = pos[idx], up[idx]
    keep = _simplify(p, tol)
    p, u = snap_to_road(p[keep], u[keep], road), u[keep]
    d = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))]).astype(np.float32)
    nxt = np.vstack([p[1:] - p[:-1], p[-1:] - p[-2:-1]])
    nxt /= np.linalg.norm(nxt, axis=1, keepdims=True)
    return p, nxt, u, d


GOAL_JUNCTION_BEFORE = 150.0  # metres before the lap's travel-index-0 point (retail: 7-310 m)


def _split(line, s_split):
    """Index of the last point before distance s_split on a line, and the interpolated point there."""
    p, d, u, dist = line
    i = int(np.clip(np.searchsorted(dist, s_split) - 1, 0, len(p) - 2))
    t = float((s_split - dist[i]) / max(dist[i + 1] - dist[i], 1e-6))
    pos = p[i] + (p[i + 1] - p[i]) * t
    dirv = d[i]
    up = u[i] + (u[i + 1] - u[i]) * t
    return i, pos, dirv, up / np.linalg.norm(up)


def _split_near(line, anchor):
    """Like _split, but at the point of the line closest to `anchor` (all junction split points
    sit within a few metres of each other on retail stages)."""
    p, d, u, dist = line
    a, b = p[:-1].astype(np.float64), p[1:].astype(np.float64)
    ab = b - a
    t = np.clip(np.einsum("ij,ij->i", anchor - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-9), 0, 1)
    q = a + ab * t[:, None]
    i = int(np.argmin(np.linalg.norm(q - anchor, axis=1)))
    up = u[i] + (u[i + 1] - u[i]) * t[i]
    return i, q[i], d[i], up / np.linalg.norm(up)


def _distances(pos: np.ndarray) -> np.ndarray:
    return np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pos, axis=0), axis=1))]).astype(np.float32)


def convert_compath(track: AsrTrack, format_bytes: bytes, stage: str, stage_bytes: bytes | None = None,
                    with_goal: bool = False) -> ComPath:
    """MAIN (and optionally GOAL) paths; "in" = ASR racing line, "out" = ASR safe line.

    Without GOAL: MAIN is one closed lap in stg1901's single-path layout (works in game).
    With GOAL: retail layout as on stg1034 -- MAIN runs a lap plus an overlap past the line,
    GOAL covers the final stretch and ends past the line at the travel ring, and a kind-3
    "GOAL1-1" junction splits both into MAIN_0/MAIN_1 and GOAL_0/GOAL_1, with the routing
    section written by cw.compath.encode_routing.
    """
    fmt = parse_compath(format_bytes)
    if len(fmt.paths) != 1 or fmt.junctions_raw.strip(b"\0"):
        raise ValueError("ComPath format template must be a single MAIN path without junctions (e.g. stg1901)")
    main_t = fmt.paths[0]
    road = track.road_triangles
    loop_len = _resample(track.racing, track.ups, 1.0)[4]

    def lines(s0, s1):
        return [_open_line(loop, track.ups, road, s0, s1) for loop in (track.racing, track.safe)]

    if not with_goal:
        ln = lines(0.0, loop_len - 1.0)
        paths, segments = [], []
        for lst, suffix in ((paths, ""), (segments, "_0")):
            lst.append(CPath("MAIN" + suffix, f"{stage}_MAINin{suffix}", f"{stage}_MAINout{suffix}",
                             float(ln[0][3][-1]), float(ln[1][3][-1]), main_t.path_type, main_t.unknown,
                             ln[0][3], ln[1][3], main_t.reserved))
        pts = np.zeros(sum(len(c[0]) for c in ln), POINT_DTYPE)
        pts["pos"] = np.vstack([c[0] for c in ln])
        pts["dir"] = np.vstack([c[1] for c in ln])
        pts["up"] = np.vstack([c[2] for c in ln])
        n = len(pts)
        return ComPath(pts, paths, segments, fmt.junctions_raw,
                       np.arange(n, dtype=np.int32), np.arange(n, dtype=np.int32), fmt.tail)

    goal_t = None
    if stage_bytes:
        goal_t = next((p for p in parse_compath(stage_bytes).paths if p.name.upper() == "GOAL"), None)
    goal_name, goal_in, goal_out = ("GOAL", goal_t.in_line, goal_t.out_line) if goal_t else         ("GOAL", f"{stage}_GOAL_IN", f"{stage}_GOAL_OUT")

    goal_start = loop_len - GOAL_BEFORE
    s_junction = loop_len - GOAL_JUNCTION_BEFORE
    main_lines = lines(0.0, loop_len + MAIN_OVERLAP)              # [in, out]
    goal_lines = lines(goal_start, loop_len + GOAL_AFTER)          # [in, out]
    all_lines = main_lines + goal_lines                            # MAIN in, MAIN out, GOAL in, GOAL out

    # Point layout: path line points first, then the junction's 4 split points.
    bases, n_path_points = [], 0
    for ln in all_lines:
        bases.append(n_path_points)
        n_path_points += len(ln[0])
    anchor = _split(all_lines[0], s_junction)[1].astype(np.float64)
    splits = [_split_near(ln, anchor) for ln in all_lines]
    extra_idx = [n_path_points + k for k in range(4)]

    pts = np.zeros(n_path_points + 4, POINT_DTYPE)
    pts["pos"] = np.vstack([ln[0] for ln in all_lines] + [np.array([sp[1] for sp in splits])])
    pts["dir"] = np.vstack([ln[1] for ln in all_lines] + [np.array([sp[2] for sp in splits])])
    pts["up"] = np.vstack([ln[2] for ln in all_lines] + [np.array([sp[3] for sp in splits])])
    P = pts["pos"].astype(np.float64)

    def halves(k):
        """Point indices of line k before and after the junction, each including the split point."""
        i = splits[k][0]
        first = list(range(bases[k], bases[k] + i + 1)) + [extra_idx[k]]
        second = [extra_idx[k]] + list(range(bases[k] + i + 1, bases[k] + len(all_lines[k][0])))
        return first, second

    seg_idx, seg_paths = [], []
    for name, in_name, out_name, ptype, unknown, reserved, k_in in (
            ("MAIN", f"{stage}_MAINin", f"{stage}_MAINout", main_t.path_type, main_t.unknown, main_t.reserved, 0),
            (goal_name, goal_in, goal_out, 7, 3, (0, 0, 0), 2)):
        in_halves, out_halves = halves(k_in), halves(k_in + 1)
        for part, (start, end) in enumerate(((-1, 0), (0, -1))):
            a, b = in_halves[part], out_halves[part]
            da, db = _distances(P[a]), _distances(P[b])
            seg_idx.append((a, b, start, end))
            seg_paths.append(CPath(f"{name}_{part}", f"{in_name}_{part}", f"{out_name}_{part}",
                                   float(da[-1]), float(db[-1]), ptype, unknown, da, db, reserved))

    paths = [
        CPath("MAIN", f"{stage}_MAINin", f"{stage}_MAINout", float(main_lines[0][3][-1]),
              float(main_lines[1][3][-1]), main_t.path_type, main_t.unknown,
              main_lines[0][3], main_lines[1][3], main_t.reserved),
        CPath(goal_name, goal_in, goal_out, float(goal_lines[0][3][-1]), float(goal_lines[1][3][-1]),
              7, 3, goal_lines[0][3], goal_lines[1][3], (0, 0, 0)),
    ]
    junction = Junction("GOAL1-1", tuple(P[extra_idx].mean(0)), 0.93, 25.0, 3, [2, 1], [0.0, 1.0])
    # Segment order MAIN_0, MAIN_1, GOAL_0, GOAL_1 -> in (0, 2), out (1, 3), branch (GOAL) out 3.
    routing = encode_routing([junction], n_path_points, seg_idx, [(0, 2, 1, 3, 3)])
    return ComPath(pts, paths, seg_paths, b"", np.zeros(0, np.int32), np.zeros(0, np.int32), (-1, -1),
                   routing_raw=routing)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("sif", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--template", default="stg1901",
                    help="CW stage being replaced: supplies materials, markers and the start line to align to")
    ap.add_argument("--compath-format", default="stg1901",
                    help="CW stage with a single MAIN path whose ComPath layout is copied")
    ap.add_argument("--no-align", action="store_true", help="keep ASR coordinates instead of matching the template start line")
    ap.add_argument("--no-goal-path", action="store_true",
                    help="write a single closed MAIN path (stg1901 layout) instead of MAIN+GOAL")
    ap.add_argument("--finish-offset", type=float,
                    help="metres from the template's travel-index-0 waypoint to its finish line "
                         "(default: known value for the template, else 0)")
    ap.add_argument("--line-shift", type=float,
                    help="metres to move the finish line past ASR's painted line (default: just enough for a "
                         "straight, flat starting grid)")
    ap.add_argument("--scale", type=float, default=DEFAULT_SCALE,
                    help="uniform scale from ASR units to CrossWorlds metres (default %(default)s)")
    args = ap.parse_args()
    track = AsrTrack(read_sif(args.sif))
    track.scale(args.scale)
    finish = args.finish_offset if args.finish_offset is not None else FINISH_OFFSETS.get(args.template, 0.0)
    our_finish = finish - START_SHIFTS.get(args.template, 0.0)   # line, metres past our travel index 0
    line_shift = args.line_shift if args.line_shift is not None else track.straight_shift(GRID_LENGTH, our_finish)
    if line_shift:
        track.advance_line(line_shift)
        print(f"moved the finish line {line_shift:.0f} m past ASR's painted line so the starting grid is on a straight")
    finish = args.finish_offset if args.finish_offset is not None else FINISH_OFFSETS.get(args.template, 0.0)
    if not args.no_align:
        pos, d = template_start((TEMPLATE_DIR / f"{args.template}_Lap_Navigation.bin").read_bytes(), finish)
        track.align_start_to(pos, d)
        print(f"aligned ASR finish line to {args.template}'s finish ({finish} m past its start) "
              f"at {pos.round(1)} facing {d.round(3)}")
    if our_finish:
        track.shift_start(our_finish)
    args.out.mkdir(parents=True, exist_ok=True)
    # Lets other tools (e.g. the Unreal visual export) place ASR geometry exactly on the converted collision.
    (args.out / "align.json").write_text(json.dumps({
        "template": args.template, "rotation": track.rotation.tolist(), "offset": track.offset.tolist(),
        "scale": args.scale,
        "note": "cw = rotation @ asr + offset (both Y-up); rotation includes the uniform scale"}, indent=2))
    laps = ["Lap", "FinalLap"]

    col = convert_collision(track, args.template)
    data = write_collision(col)
    assert write_collision(parse_collision(data)) == data
    for lap in laps:
        # The final lap gets its own copy only if the replaced stage swaps collision on the final lap.
        if (TEMPLATE_DIR / f"{args.template}_{lap}_Collision.bin").exists():
            (args.out / f"{lap}_Collision.bin").write_bytes(data)
            # Track-only copy; pipeline.retarget_actors --collision adds moved gimmicks on top of it.
            (args.out / f"{lap}_Collision.track.bin").write_bytes(data)
    print(f"collision: {len(col.chunks)} chunks, "
          f"{sum(len(p.triangles) for c in col.chunks for p in c.parts)} triangles, {len(data)} bytes")

    template_road = drivable_triangles(parse_collision((TEMPLATE_DIR / f"{args.template}_Lap_Collision.bin").read_bytes()))
    for lap in laps:
        tpath = TEMPLATE_DIR / f"{args.template}_{lap}_Navigation.bin"
        if not tpath.exists():
            continue
        nav = convert_navigation(track, tpath.read_bytes(), template_road,
                                 start_shift=START_SHIFTS.get(args.template, 0.0))
        data = write_navigation(nav)
        assert write_navigation(parse_navigation(data)) == data
        (args.out / f"{lap}_Navigation.bin").write_bytes(data)
        print(f"navigation ({lap}): {len(nav.waypoints)} waypoints, {len(nav.markers)} markers, "
              f"length {nav.total_length:.1f}")

    fmt = (TEMPLATE_DIR / f"{args.compath_format}_Lap_ComPath.bin").read_bytes()
    own = TEMPLATE_DIR / f"{args.template}_Lap_ComPath.bin"
    cp = convert_compath(track, fmt, args.template, own.read_bytes() if own.exists() else None,
                         with_goal=not args.no_goal_path)
    data = write_compath(cp)
    if cp.routing_raw is None:
        assert write_compath(parse_compath(data)) == data
    for lap in laps:
        if (TEMPLATE_DIR / f"{args.template}_{lap}_ComPath.bin").exists():
            (args.out / f"{lap}_ComPath.bin").write_bytes(data)
    for p in cp.paths:
        print(f"compath {p.name}: in {len(p.in_distance)} pts / {p.in_length:.1f}, "
              f"out {len(p.out_distance)} pts / {p.out_length:.1f}")


if __name__ == "__main__":
    main()
