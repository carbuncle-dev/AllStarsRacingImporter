"""Headless Blender: export ASR track visuals as FBX placed for a CrossWorlds stage.

    blender -b -P tools/export_for_unreal.py -- <track.obj> <align.json> <out_dir> [<ref Collision.bin> ...]

Coordinate chain (all baked into vertex positions, so actors are placed at the origin):
    ASR (Y-up, m)  --align.json-->  CW collision space (Y-up, m)
    CW (x, y, z)   -->  Unreal (X, Y, Z) = (z, -x, y) * 100
        Verified against E-Stadium's placed actors (goal glow, trackside tyres) resting on its road
        collision. The minimap texture names fit (-z, x, y) too -- they use the opposite sign
        convention -- and using that put the visuals rotated 180 degrees in game.
Blender's FBX exporter + Unreal's FBX importer map Blender (X, Y, Z) to Unreal (X, -Y, Z),
so the Blender-side coordinates written are (z, x, y).

Outputs in <out_dir>:
    <name>_Visual.fbx      track.Forest geometry joined per material, with ASR's vertex colours
    REF_<name>.fbx         optional collision meshes (from CW Collision.bin payloads) to check alignment
"""

import glob
import json
import os
import struct
import sys

import bpy
import mathutils
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cw.collision import parse_collision  # noqa: E402

args = [os.path.abspath(a) for a in sys.argv[sys.argv.index("--") + 1:]]
obj_path, align_path, out_dir, refs = args[0], args[1], args[2], args[3:]
os.makedirs(out_dir, exist_ok=True)
align = json.load(open(align_path))
R, OFF = np.array(align["rotation"]), np.array(align["offset"])
name = os.path.splitext(os.path.basename(obj_path))[0]


def cw_to_blender(cw: np.ndarray) -> np.ndarray:
    """CW collision space -> Blender coords that land at Unreal (z, -x, y)*100 after FBX round trip."""
    return np.stack([cw[:, 2], cw[:, 0], cw[:, 1]], 1)


def export(objects, path):
    bpy.ops.object.select_all(action="DESELECT")
    for ob in objects:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objects[0]
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="X", axis_up="Z",
                             bake_space_transform=False, mesh_smooth_type="FACE",
                             path_mode="COPY", embed_textures=True, use_mesh_modifiers=False,
                             # ASR's vertex colours (baked lighting): Blender's OBJ importer reads
                             # them as sRGB, SRGB writes the original values back (0.5 stays 0.5).
                             colors_type="SRGB")
    print(f"wrote {path} ({len(objects)} objects)")


bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.obj_import(filepath=obj_path)

# Keep the visible track (it has its own skydome and clouds); drop items, cameras, shadows.
# envmap.Forest is a low-detail copy of the scenery with one baked atlas (SAMBA_H_REFLECTION),
# used by ASR only to render reflections: 87% of its vertices sit within 5 m of track geometry,
# so in game it showed through the real meshes as blurry, low-res objects.
# item.Forest holds the templates of objects ASR's logic places (balloons, loudspeakers, bats, TVs;
# pipeline.asr_objects): kept here, copied to their locators by place_item_instances.
KEEP = ("track.Forest", "item.Forest")
# Untextured materials are ASR glass, glow planes and helper geometry that rendered as solid grey
# walls in game; drop them, except real structure that just has no texture.
KEEP_UNTEXTURED = ("ferris_wheel",)


def has_texture(mat):
    return mat is not None and mat.use_nodes and any(n.type == "TEX_IMAGE" and n.image for n in mat.node_tree.nodes)


for ob in list(bpy.data.objects):
    if not ob.name.startswith(KEEP) or ob.type != "MESH":
        bpy.data.objects.remove(ob, do_unlink=True)
        continue
    mat = ob.data.materials[0] if ob.data.materials else None
    if not has_texture(mat) and not (mat and any(k in mat.name.lower() for k in KEEP_UNTEXTURED)):
        bpy.data.objects.remove(ob, do_unlink=True)

# ASR's vertex colours with alpha, exact: <obj>.vcol (forest_to_obj) holds x, y, z, r, g, b, a per
# OBJ "v" line. They travel as two extra UV maps (VColRG, VColBA) -- UVs are plain floats, while the
# colour attribute went through colour management and lost alpha. Objects are matched to their run of
# records by name (OBJ "o" order; Blender suffixes repeated names .001, .002, ... in that order) and
# checked against the positions.
def attach_vertex_colours():
    vcol_path = os.path.splitext(obj_path)[0] + ".vcol"
    if not os.path.exists(vcol_path):
        print("no .vcol next to the OBJ; vertex colours not attached")
        return
    rec = np.fromfile(vcol_path, np.float32).reshape(-1, 7).astype(np.float64)  # same type as the keys below
    runs, start = [], 0
    run_mat = []
    for line in open(obj_path, errors="replace"):
        if line.startswith("o "):
            runs.append([start, 0])
            run_mat.append("")
        elif line.startswith("usemtl ") and runs:
            run_mat[-1] = line[7:].strip()
        elif line.startswith("v ") and runs:
            runs[-1][1] += 1
            start += 1
    if start != len(rec):
        raise SystemExit(f"{vcol_path}: {len(rec)} records for {start} OBJ vertices")
    # Blender's names for repeated "o" names don't follow file order: key the runs by vertex count
    # and first vertex instead, then check every position.
    by_key, by_count = {}, {}
    run_index = {s: i for i, (s, n) in enumerate(runs)}
    material_of = {s: run_mat[i] for i, (s, n) in enumerate(runs)}
    used = set()
    for s, n in runs:
        if n:
            by_key.setdefault((n, tuple(np.round(rec[s, :3], 2))), []).append(s)
            by_count.setdefault(n, []).append(s)
    bad, unmatched = 0, __import__("collections").Counter()
    by_material, derived = {}, []

    def set_colours(me, colours):
        loops = np.empty(len(me.loops), np.int64)
        me.loops.foreach_get("vertex_index", loops)
        for layer, (a, b) in (("VColRG", (0, 1)), ("VColBA", (2, 3))):
            uv = me.uv_layers.new(name=layer)
            uv.data.foreach_set("uv", colours[loops][:, [a, b]].astype(np.float32).ravel())

    for ob in bpy.data.objects:
        me = ob.data
        if "vcol_from" in ob:  # generated geometry lit like the given materials' meshes (mean colour)
            derived.append(ob)
            continue
        co = np.empty(len(me.vertices) * 3, np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)  # local coordinates are the OBJ's (the axis change is in matrix_world)
        colours = None
        if len(co):
            candidates = by_key.get((len(co), tuple(np.round(co[0], 2))), []) + by_count.get(len(co), [])
            # Identical geometry can occur under different materials (Rocky Coaster's orange and red
            # balloon templates): prefer a run with the object's own material, and use each run once.
            mat = me.materials[0].name.split(".")[0] if me.materials and me.materials[0] else ""
            candidates = sorted(dict.fromkeys(candidates), key=lambda s: (s in used, material_of[s] != mat))
            for s in candidates:
                # the OBJ text keeps 6 significant digits: far-out geometry is off by millimetres
                if np.abs(co - rec[s:s + len(co), :3]).max() < 0.05:
                    used.add(s)
                    colours = rec[s:s + len(co), 3:]
                    ob["asr_mesh"] = run_index[s]  # index into <obj>.materials.json "meshes"
                    if me.materials and me.materials[0]:
                        by_material.setdefault(me.materials[0].name, []).append(colours)
                    break
        if colours is None:
            bad += 1
            unmatched[ob.data.materials[0].name if ob.data.materials and ob.data.materials[0] else "none"] += 1
            colours = np.tile([0.5, 0.5, 0.5, 1.0], (len(co), 1))
        set_colours(me, colours)
    for ob in derived:
        src = [c for m in ob["vcol_from"] for c in by_material.get(m, [])]
        mean = np.concatenate(src).mean(0) if src else np.array([0.5, 0.5, 0.5, 1.0])
        mean[3] = 1.0
        set_colours(ob.data, np.tile(mean, (len(ob.data.vertices), 1)))
        print(f"{ob.name}: vertex colour {np.round(mean, 3).tolist()} (mean of {list(ob['vcol_from'])})")
    print(f"vertex colours attached ({bad} objects unmatched, left neutral): {unmatched.most_common(12)}")


def close_cloud_floor():
    """Rocky Coaster's cloud sea is rings of cloud cards rising outward, with an open centre right under
    the course (y ~ -306 in CW space, ~311 m across): the stage intro's overhead camera saw black
    through it. Fill it with an opaque disc ("cloud_floor": pipeline.cloud_floor's non-repeating cloud-sea
    texture, mapped once; vertex colour = the rings' mean), just below the lowest ring, built in the
    OBJ's coordinates like the imported meshes."""
    ring_mat = "lambert15"  # CLOUDS.TGA, the innermost lowest rings
    mat = bpy.data.materials.get(ring_mat)
    inner = {bpy.data.materials.get(n) for n in ("lambert14", "lambert15")} - {None}  # the two lowest ring sets
    ring = [ob for ob in bpy.data.objects if ob.type == "MESH" and ob.data.materials and ob.data.materials[0] in inner]
    if not ring:
        return
    pts = []
    for ob in ring:
        co = np.empty(len(ob.data.vertices) * 3)
        ob.data.vertices.foreach_get("co", co)
        pts.append(co.reshape(-1, 3))  # local = OBJ (ASR) coordinates
    asr = np.concatenate(pts)
    cw = asr @ R.T + OFF
    centre = cw.mean(0)
    radius = float(np.hypot(*(cw[:, [0, 2]] - centre[[0, 2]]).T).max())
    floor_y = float(cw[:, 1].min()) - 1.0
    n = 96
    ang = np.linspace(0, 2 * np.pi, n, endpoint=False)
    rim = np.stack([centre[0] + radius * np.cos(ang), np.full(n, floor_y), centre[2] + radius * np.sin(ang)], 1)
    cw_pts = np.vstack([[centre[0], floor_y, centre[2]], rim])
    obj_pts = (cw_pts - OFF) @ np.linalg.inv(R).T  # back to OBJ (ASR) coordinates
    me = bpy.data.meshes.new("cloud_floor")
    faces = [(0, 1 + (i + 1) % n, 1 + i) for i in range(n)]
    me.from_pydata(obj_pts.tolist(), [], faces)
    if np.array(me.polygons[0].normal) @ (np.linalg.inv(R) @ np.array([0.0, 1.0, 0.0])) < 0:  # face up (sky)
        me.from_pydata([], [], [])
        me = bpy.data.meshes.new("cloud_floor")
        me.from_pydata(obj_pts.tolist(), [], [(0, 1 + i, 1 + (i + 1) % n) for i in range(n)])
    uv = me.uv_layers.new(name="UVMap")
    loops = np.empty(len(me.loops), np.int64)
    me.loops.foreach_get("vertex_index", loops)
    planar = (cw_pts[:, [0, 2]] - centre[[0, 2]]) / (2 * radius) + 0.5  # pipeline.cloud_floor: once, no repeat
    uv.data.foreach_set("uv", planar[loops].astype(np.float32).ravel())
    floor_mat = mat.copy()
    floor_mat.name = "cloud_floor"
    me.materials.append(floor_mat)
    ob = bpy.data.objects.new("track.Forest_cloud_floor", me)
    ob["vcol_from"] = sorted(m.name for m in inner)  # lit like the rings around it
    bpy.context.scene.collection.objects.link(ob)
    ob.matrix_world = ring[0].matrix_world.copy()  # same OBJ-import axis change as the other meshes
    print(f"cloud floor: radius {radius:.0f} m at y {floor_y:.0f} (CW)")


close_cloud_floor()
attach_vertex_colours()


def quat_matrix(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


# item.Forest trees placed on the road that racers pop (orange and red balloons): their animated export
# also gets ASR's pop (<label>_Hit.fbx) and the idle loop's extent (<label>.bounds.json) for the break
# objects (ue_build_level)
BREAKABLE_TREES = (4, 5)
HIT_EFFECTS = ("burst", "flash", "soundwave", "blinn2")  # pop sprites (T_Balloon_Burst, FLASH...), the loudspeakers' pulse (SOUND_WAVE, HEAT_HAZE planes ~25 m wide, scaled by the animation)


def place_item_instances():
    """Copies of item.Forest trees at ASR's logic locators (<obj>.locators.json, pipeline.asr_objects):
    vertices moved by the locator's rotation and position in the OBJ's (ASR) coordinates; each copy
    keeps its mesh metadata and records ["item_instance"] (locator index) for the animated export."""
    path = os.path.splitext(obj_path)[0] + ".locators.json"
    base = os.path.splitext(obj_path)[0]
    meshes = json.load(open(base + ".materials.json"))["meshes"] if os.path.exists(base + ".materials.json") else []
    templates = {}
    for ob in list(bpy.data.objects):
        if ob.name.startswith("item.Forest"):
            meta = meshes[ob["asr_mesh"]] if "asr_mesh" in ob and meshes else None
            if meta:
                templates.setdefault(meta["tree"], []).append(ob)
            else:
                bpy.data.objects.remove(ob, do_unlink=True)
    locators = json.load(open(path)) if os.path.exists(path) else []
    placed = 0
    for i, loc in enumerate(locators):
        rot, pos = quat_matrix(loc["rotation"]), np.array(loc["position"], np.float64)
        for tmpl in templates.get(loc["tree"], []):
            mat = tmpl.data.materials[0].name.lower() if tmpl.data.materials and tmpl.data.materials[0] else ""
            hit_effect = any(k in mat for k in HIT_EFFECTS)
            if hit_effect and loc["tree"] not in BREAKABLE_TREES and "soundwave" not in mat:
                continue  # shown by the "Hit" animation only; the looping "Auto" one leaves them visible
            # The loudspeakers' sound wave (SOUND_WAVE) stays: placed copies play their loop without scale and
            # visibility (which decode wrongly, see asr.anim.branch_worlds), so it shows at rest size and
            # scrolls out of the speaker with ASR's texture animation. Its heat-haze ring (blinn2) stays out.
            ob = tmpl.copy()
            ob.data = tmpl.data.copy()
            ob.name = f"track.Forest_item{i}_t{loc['tree']}"
            co = np.empty(len(ob.data.vertices) * 3, np.float64)
            ob.data.vertices.foreach_get("co", co)
            ob.data.vertices.foreach_set("co", (co.reshape(-1, 3) @ rot.T + pos).astype(np.float32).ravel())
            ob["item_instance"] = i
            ob["hit_effect"] = hit_effect  # breakable objects keep them for their "Hit" animation
            bpy.context.scene.collection.objects.link(ob)
            placed += 1
    for obs in templates.values():
        for ob in obs:
            mesh = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            if mesh.users == 0:
                bpy.data.meshes.remove(mesh)
    print(f"placed {len(locators)} logic objects ({placed} pieces) from {os.path.basename(path)}")


place_item_instances()


# Pieces whose texture region has soft alpha (glows, halos) while their material is a cut-out (the
# sun's glow shares an atlas and material with its hard-edged rays, and showed as a solid white
# disc): give them "<material>__soft", which the Unreal build makes translucent with the same texture.
SOFT_PIECE = 0.3  # share of partially transparent texels inside the piece's UV area


def split_soft_pieces():
    kinds_path = os.path.join(out_dir, "materials.json")  # pipeline.unreal_materials
    kinds = {k: v.get("kind") for k, v in json.load(open(kinds_path)).items()} if os.path.exists(kinds_path) else {}
    alpha_cache, soft_mats, moved = {}, {}, 0
    opaque_mats, unhidden = {}, 0
    asr_mats = json.load(open(os.path.splitext(obj_path)[0] + ".materials.json"))["materials"]         if os.path.exists(os.path.splitext(obj_path)[0] + ".materials.json") else {}
    for ob in bpy.data.objects:
        mat = ob.data.materials[0] if ob.data.materials else None
        if mat is None or kinds.get(mat.name) != "masked":  # translucent/water ones already blend
            continue
        img = next((n.image for n in mat.node_tree.nodes if n.type == "TEX_IMAGE" and n.image), None) \
            if mat and mat.use_nodes else None
        if img is None or not ob.data.uv_layers:
            continue
        if img.name not in alpha_cache:
            w, h = img.size
            px = np.empty(w * h * 4, np.float32)
            img.pixels.foreach_get(px)
            alpha_cache[img.name] = px.reshape(h, w, 4)[..., 3]
        a = alpha_cache[img.name]
        if (a < 0.98).mean() < 0.001:  # opaque texture
            continue
        uv = np.empty(len(ob.data.loops) * 2, np.float32)
        ob.data.uv_layers[0].data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2) % 1.0
        h, w = a.shape
        x0, x1 = int(uv[:, 0].min() * w), int(np.ceil(uv[:, 0].max() * w))
        y0, y1 = int(uv[:, 1].min() * h), int(np.ceil(uv[:, 1].max() * h))
        region = a[y0:max(y1, y0 + 1), x0:max(x1, x0 + 1)]
        # A piece whose whole texture area is fully transparent would be invisible as a cut-out. On
        # Rocky Coaster's signboard atlas (BOARD01) the alpha is a glow mask for the neon signs only, so
        # every painted sign vanished. ASR materials without the blend (0x10) or alpha-test (0x40)
        # shader bits ignore alpha: such pieces get the opaque variant ("<material>__opaque").
        flags = asr_mats.get(mat.name, {}).get("psflags", 0x10)
        if region.size and region.max() < 0.02 and not flags & 0x50:
            if mat.name not in opaque_mats:
                opaque_mats[mat.name] = mat.copy()
                opaque_mats[mat.name].name = mat.name + "__opaque"
            ob.data.materials[0] = opaque_mats[mat.name]
            unhidden += 1
            continue
        if region.size == 0 or ((region > 0.06) & (region < 0.94)).mean() <= SOFT_PIECE:
            continue
        if mat.name not in soft_mats:
            soft_mats[mat.name] = mat.copy()
            soft_mats[mat.name].name = mat.name + "__soft"
        ob.data.materials[0] = soft_mats[mat.name]
        moved += 1
    print(f"soft pieces: {moved} objects moved to {sorted(m.name for m in soft_mats.values())}")
    print(f"fully transparent cut-out pieces made opaque: {unhidden} ({sorted(opaque_mats)})")


split_soft_pieces()


# ASR scrolls textures with UV bones (waterfalls: per piece, along U, +-0.3..0.5 per second); a piece
# uses the UV bone its diffuse texture matrix points at. The rate goes in a "Pan" UV map (UV channel 3
# in Unreal; zero elsewhere), read by the translucent material. Rates are the median step per frame
# (some channels are sawtooths that wrap), at the 30 fps of the forest animations.
def uv_motion(values: np.ndarray):
    """ASR UV-bone translation over frames -> ("pan", rate (u, v) per s) or ("flip", period s, phase, nu, nv).

    Flipbooks step through an atlas (the coloured light bulbs: 3 columns, sometimes 3 rows): a few
    distinct offsets, constant between jumps. Index n = floor(t / period + phase) gives
    u = frac(n / nu), v = frac(floor(n / nu) / nv)."""
    q = np.round(values, 2)
    us, vs = sorted(set(q[:, 0])), sorted(set(q[:, 1]))
    changes = np.flatnonzero(np.any(np.abs(np.diff(values, axis=0)) > 0.05, axis=1))
    if 1 < len(us) * len(vs) <= 16 and len(changes) >= 2 and len(changes) < len(values) / 4:
        runs = np.diff(changes)
        period = float(np.median(runs)) / ANIM_FPS
        nu, nv = len(us), len(vs)
        iu = int(round(values[0, 0] * nu)) % nu
        iv = int(round(values[0, 1] * nv)) % nv
        phase = (iu + nu * iv) - (changes[0] + 1) / ANIM_FPS / period % 1.0 + 1.0  # index at t = 0
        return ("flip", period, phase, nu, nv)
    rate = np.median(np.diff(values, axis=0), axis=0)
    ratchet = uv_ratchet(values)
    if ratchet:
        return ratchet
    net = (values[-1] - values[0]) / max(1, len(values) - 1)
    # Slides with holds (the billboard's screen moves to the next frame, then pauses): the median step is
    # 0 and the screen stood still; such pieces scroll at their average rate instead.
    rate = np.where(np.abs(rate) < 1e-6, net, rate)
    return ("pan", rate * ANIM_FPS)


def uv_ratchet(values: np.ndarray):
    """Ratchets (the billboard's screen: six images stacked along V; the loop holds each one, then slides the
    next one in): ("flip", ...) over the held images (sliding along V, else switching halfway through), or None.
    (Averaged into a scroll the screen crept ~0.004 per second: still in game.)"""
    for axis in (0, 1):
        v, other = values[:, axis], values[:, 1 - axis]
        if np.ptp(other) > 1e-3:
            continue
        speed = np.abs(np.diff(v))
        moving = speed > 0.15 * speed.max()  # the holds wobble slightly (spline overshoot)
        if moving.mean() > 0.7 or not moving.any():
            continue
        levels = []
        for x in np.sort(np.round(v[:-1][~moving] % 1.0, 3)):
            if not levels or x - levels[-1] > 0.01:
                levels.append(x)
        if len(levels) > 1 and 1.0 - levels[-1] + levels[0] < 0.01:
            levels.pop()  # 1.0 is 0 again
        if len(levels) < 2:
            continue
        frames = int(round(1.0 / min(np.diff(levels))))
        if frames > 16 or any(abs(x * frames - round(x * frames)) > 0.05 for x in levels):
            continue
        # images stepped through per loop: the net change (the billboard: 0 -> 1 = all six), else the slides
        steps = int(round(abs(v[-1] - v[0]) * frames))
        slides = steps if steps else int(np.sum(moving[1:] & ~moving[:-1]) + moving[0])
        period = (len(values) - 1) / ANIM_FPS / max(1, slides)
        first = int(round(v[0] * frames))
        if axis == 1:
            # the slide itself (in game an instant switch looked wrong): from leaving one image to resting on
            # the next, as a share of each step, passed in the fractional part of the row count (nv + slide / 2,
            # ue_build_level.texture_uv eases between images over it); the step ends with the slide
            step = np.sign(v[-1] - v[0]) / frames
            start = int(np.argmax(np.abs(v - v[0]) >= 0.02 / frames))
            end = start + int(np.argmax(np.abs(v[start:] - (v[0] + step)) <= 0.02 / frames))
            slide = min(0.98, (end - start) / ANIM_FPS / period)
            return ("flip", period, first + 1.0 - end / ANIM_FPS / period, 1, frames + slide / 2)
        switch = int(np.argmax(np.abs(v - v[0]) >= 0.5 / frames)) / ANIM_FPS
        return ("flip", period, first + 1.0 - switch / period, frames, 1)
    return None


def attach_texture_motion():
    """Per-piece texture animation as UV maps: "Pan" (UV3: rate u, v per s), "Flip1" (UV4: period,
    phase) and "Flip2" (UV5: columns, rows; zero = no flipbook), read by the Unreal materials.
    "Pan2"/"Pan3" (UV6/UV7): scroll rates of texture slots 1 and 2 (each slot has its own texture
    matrix / UV bone, forest_to_obj "matrix_indices"): the waterfalls' second streak and sparkle layers."""
    base = os.path.splitext(obj_path)[0]
    motion, layer_pans = {}, {}
    if os.path.exists(base + ".anims.json") and os.path.exists(base + ".materials.json"):
        from asr.anim import decode
        meshes = json.load(open(base + ".materials.json"))["meshes"]
        uv = {}
        for tree in json.load(open(base + ".anims.json"))["trees"]:
            if tree["forest_name"] not in ("track.Forest", "item.Forest") or not tree["animations"]:
                continue
            for (kind, index, channel), values in decode(tree["animations"][0]).items():
                if kind == "uv" and channel == 1 and len(values) > 1:
                    uv[(tree["forest_name"], tree["tree"], index)] = uv_motion(values)
        for ob in bpy.data.objects:
            meta = meshes[ob["asr_mesh"]] if "asr_mesh" in ob else None
            if not meta:
                continue
            # placed item copies (the speaker's sound waves...) keep item.Forest's tree numbers
            forest = "item.Forest" if meta["name"].startswith("item.Forest") else "track.Forest"
            if meta["uv_matrix"] != 255 and (forest, meta["tree"], meta["matrix_index"]) in uv:
                motion[ob.name] = uv[(forest, meta["tree"], meta["matrix_index"])]
            for slot in (1, 2):
                idx = meta["matrix_indices"][slot] if "matrix_indices" in meta else 255
                m = uv.get((forest, meta["tree"], idx)) if idx != 255 else None
                if m and m[0] == "pan":
                    layer_pans[(ob.name, slot)] = m[1]
    kinds = __import__("collections").Counter()
    for ob in bpy.data.objects:
        m = motion.get(ob.name)
        pan, flip1, flip2 = np.zeros(2), np.zeros(2), np.zeros(2)
        if m and m[0] == "pan" and np.abs(m[1]).max() > 1e-4:
            pan = m[1]
            kinds["pan"] += 1
        elif m and m[0] == "flip":
            flip1, flip2 = np.array([m[1], m[2]]), np.array([m[3], m[4]])
            kinds["flip"] += 1
        n = len(ob.data.loops)
        pan2, pan3 = (layer_pans.get((ob.name, k), np.zeros(2)) for k in (1, 2))
        kinds["layer pans"] += (ob.name, 1) in layer_pans or (ob.name, 2) in layer_pans
        for name, value in (("Pan", pan), ("Flip1", flip1), ("Flip2", flip2), ("Pan2", pan2), ("Pan3", pan3)):
            ob.data.uv_layers.new(name=name).data.foreach_set("uv", np.tile(value.astype(np.float32), n))
    print(f"texture motion: {dict(kinds)}; flipbooks "
          f"{sorted({(round(m[1], 2), m[3], m[4]) for m in motion.values() if m[0] == 'flip'})}")


ANIM_FPS = 30  # CppSLib's playback rate for forest animations
attach_texture_motion()

# Bake ASR -> CW -> Unreal-facing Blender coordinates into the vertices.
for ob in bpy.data.objects:
    me = ob.data
    co = np.empty(len(me.vertices) * 3, np.float64)
    me.vertices.foreach_get("co", co)
    b0 = co.reshape(-1, 3) @ np.array(ob.matrix_world)[:3, :3].T + np.array(ob.matrix_world)[:3, 3]
    asr = np.stack([b0[:, 0], b0[:, 2], -b0[:, 1]], 1)        # OBJ import gave Blender (x, -z, y)
    out = cw_to_blender(asr @ R.T + OFF)
    me.vertices.foreach_set("co", out.astype(np.float32).ravel())
    ob.matrix_world.identity()
    # No normal flip: ASR -> CW is a rotation and CW -> these Blender coords is too (det +1);
    # the handedness change happens in the FBX -> Unreal import. (Flipping here made every
    # face point inward in-game.)
    me.update()

# --- Animated trees ------------------------------------------------------------------------------
# ASR animates whole forest trees (ferris wheel, balloons, cable cars, gears, seagulls, ...): each
# tree with bone animation becomes one skinned mesh with a bone per branch, exported with its
# animation to <name>_Anim_t<tree>.fbx. Bones (no hierarchy) rest at their branch's rest transform and
# carry, per frame, the branch's animated world transform (M W_anim W_rest^-1 relative to the rest,
# M = ASR -> these Blender coordinates). Hidden branches get ~zero scale. asr/anim.py decodes the animation data (<obj>.anims.json from forest_to_obj).
ANIM_FPS = 30  # CppSLib's playback rate for forest animations
P = np.array([[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])  # cw_to_blender
M = np.eye(4)
M[:3, :3] = P @ R
M[:3, 3] = P @ OFF
M_inv = np.linalg.inv(M)


def matrix_to_quat(m: np.ndarray) -> np.ndarray:
    """(N, 3, 3) rotation -> (N, 4) wxyz (Blender order)."""
    w = np.sqrt(np.maximum(0.0, 1 + m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2])) / 2
    x = np.sqrt(np.maximum(0.0, 1 + m[:, 0, 0] - m[:, 1, 1] - m[:, 2, 2])) / 2
    y = np.sqrt(np.maximum(0.0, 1 - m[:, 0, 0] + m[:, 1, 1] - m[:, 2, 2])) / 2
    z = np.sqrt(np.maximum(0.0, 1 - m[:, 0, 0] - m[:, 1, 1] + m[:, 2, 2])) / 2
    x = np.copysign(x, m[:, 2, 1] - m[:, 1, 2])
    y = np.copysign(y, m[:, 0, 2] - m[:, 2, 0])
    z = np.copysign(z, m[:, 1, 0] - m[:, 0, 1])
    q = np.stack([w, x, y, z], 1)
    # keep consecutive quaternions in the same hemisphere (no flips when interpolating)
    for i in range(1, len(q)):
        if np.dot(q[i], q[i - 1]) < 0:
            q[i] = -q[i]
    return q


def closest_rotation(a: np.ndarray) -> np.ndarray:
    u, _, vt = np.linalg.svd(a)
    r = u @ vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u @ vt
    return r


def polar(a: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(F, 3, 3) -> closest rotations (F, 3, 3) and per-axis scales (F, 3) (shear dropped)."""
    u, _, vt = np.linalg.svd(a)
    r = u @ vt
    neg = np.linalg.det(r) < 0
    u[neg, :, -1] *= -1
    r = u @ vt
    return r, np.einsum("fji,fji->fi", r, a)


def fcurve(action, ob, path, index):
    if hasattr(action, "fcurve_ensure_for_datablock"):  # Blender 4.4+ (layered actions)
        return action.fcurve_ensure_for_datablock(ob, path, index=index)
    return action.fcurves.new(path, index=index)


HIT_FRAMES = 60  # breakable objects' "Hit" animation, cut (ASR's pop is over by frame 16)


def write_action(arm_ob, action_name, bones, basis, frames):
    """Action with one constant key per frame and bone from per-frame matrices relative to the bone's rest."""
    action = bpy.data.actions.new(action_name)
    arm_ob.animation_data.action = action
    fr = np.arange(frames, dtype=np.float64)
    for b in bones:
        pb = arm_ob.pose.bones[f"b{b}"]
        pb.rotation_mode = "QUATERNION"
        k = basis[b]
        loc = k[:, :3, 3]
        rotm, scale = polar(k[:, :3, :3])
        rot = matrix_to_quat(rotm)
        for path, values in ((f'pose.bones["b{b}"].location', loc), (f'pose.bones["b{b}"].rotation_quaternion', rot),
                             (f'pose.bones["b{b}"].scale', scale)):
            for i in range(values.shape[1]):
                fc = fcurve(action, arm_ob, path, i)
                fc.keyframe_points.add(frames)
                fc.keyframe_points.foreach_set("co", np.stack([fr, values[:, i]], 1).astype(np.float32).ravel())
                fc.keyframe_points.foreach_set("interpolation", [0] * frames)  # constant: one key per frame
                fc.update()
    return action


def export_skinned(arm_ob, mesh_ob, path, frames):
    scene = bpy.context.scene
    scene.render.fps = ANIM_FPS
    scene.frame_start, scene.frame_end = 0, frames - 1
    bpy.ops.object.select_all(action="DESELECT")
    arm_ob.select_set(True)
    mesh_ob.select_set(True)
    bpy.context.view_layer.objects.active = arm_ob
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, apply_unit_scale=True,
                             apply_scale_options="FBX_SCALE_UNITS", axis_forward="X", axis_up="Z",
                             bake_space_transform=False, mesh_smooth_type="FACE", path_mode="COPY",
                             embed_textures=True, use_mesh_modifiers=False, colors_type="SRGB",
                             add_leaf_bones=False, primary_bone_axis="Y", secondary_bone_axis="X",
                             armature_nodetype="NULL", bake_anim=True, bake_anim_use_all_bones=True,
                             bake_anim_use_nla_strips=False, bake_anim_use_all_actions=False,
                             bake_anim_force_startend_keying=True, bake_anim_step=1.0,
                             bake_anim_simplify_factor=0.0)


def export_animated_trees() -> set:
    base = os.path.splitext(obj_path)[0]
    if not (os.path.exists(base + ".anims.json") and os.path.exists(base + ".materials.json")):
        return set()
    from asr.anim import branch_worlds
    anims = json.load(open(base + ".anims.json"))
    meshes = json.load(open(base + ".materials.json"))["meshes"]
    trees = {t["tree"]: t for t in anims["trees"] if t["forest_name"] == "track.Forest"}
    item_trees = {t["tree"]: t for t in anims["trees"] if t["forest_name"] == "item.Forest"}
    loc_path = base + ".locators.json"
    locators = json.load(open(loc_path)) if os.path.exists(loc_path) else []
    groups = {}   # key -> (tree, members, locator transform in ASR space or None, label)
    for ob in bpy.data.objects:
        meta = meshes[ob["asr_mesh"]] if "asr_mesh" in ob else None
        if not meta or meta["branch"] < 0:
            continue
        if "item_instance" in ob:
            i = ob["item_instance"]
            if meta["tree"] in item_trees:
                L = np.eye(4)
                L[:3, :3] = quat_matrix(locators[i]["rotation"])
                L[:3, 3] = locators[i]["position"]
                key = ("i", i)
                groups.setdefault(key, (item_trees[meta["tree"]], [], L, f"Anim_t{meta['tree']}_i{i}"))[1].append((ob, meta["branch"]))
        elif meta["name"].startswith("track.Forest") and meta["tree"] in trees:
            key = ("t", meta["tree"])
            groups.setdefault(key, (trees[meta["tree"]], [], None, f"Anim_t{meta['tree']}"))[1].append((ob, meta["branch"]))
    exported = set()
    only = {int(t) for t in os.environ.get("ANIM_TREES", "").split(",") if t}  # debugging: export these only
    for key, (tree, members, L, label) in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        tree_id = tree["tree"]
        if only and tree_id not in only:
            continue
        # placed objects play their looping "Auto" animation (balloons list "Start" first)
        anim = next((a for a in tree["animations"] if a["name"] == "Auto"), tree["animations"][0])
        rest, world, visible = branch_worlds(tree, anim, motion_only=L is not None)
        if L is not None:  # item template: tree space -> the locator's place in the track
            rest = L @ rest
            world = L @ world
        breakable = L is not None and tree_id in BREAKABLE_TREES
        if breakable:
            # The star balloons' idle loop decodes badly (it lingers at the bottom, jumps at the loop point and
            # wobbles +-5 degrees about the up axis; in game the bob looked irregular and some balloons turned):
            # a steady sine bob instead, the decoded loop's height and length, starting at the bottom (where the
            # break object's collision box sits), the frame-0 pose otherwise (no rotation).
            bones_all = sorted({b for _, b in members})
            height = max(float(np.ptp(world[:, b, 1, 3])) for b in bones_all)
            phase = np.arange(len(world)) / len(world)
            lift = height * (1 - np.cos(2 * np.pi * phase)) / 2
            steady = np.repeat(world[:1], len(world), axis=0)
            steady[:, :, 1, 3] += lift[:, None]
            world = steady
            # the pop sprites show in "Hit" only (Auto's type-4 visibility decodes swapped: forced off)
            for ob, b in members:
                if ob.get("hit_effect"):
                    visible[:, b] = False
        frames = anim["frames"]
        bones = sorted({b for _, b in members})
        moving = [b for b in bones if np.abs(world[:, b] - world[:1, b]).max() > 1e-4 or not visible[:, b].all()]
        if not moving and not breakable:
            continue
        # skin matrices per frame and bone, in these Blender coordinates
        # Each bone rests at its piece (rest position and rotation): ASR
        # shears some pieces (rotation under a non-uniformly scaled parent), which loc/rot/scale bones
        # cannot hold; with bones at the origin that error scaled with the distance from the origin
        # (graveyard cut-outs flew hundreds of metres off), at the piece it is negligible.
        bind = {}
        basis = {}
        for b in bones:
            # skin transform (scale-free: M also carries the track's scale, which cancels here)
            skin = M @ world[:, b] @ np.linalg.inv(rest[b]) @ M_inv
            # bone rest: the piece's position and rotation in these coordinates, without scale
            # (Blender bones drop it; a 0.5 left in made every location key twice too long)
            bind[b] = np.eye(4)
            bind[b][:3, :3] = closest_rotation((M @ rest[b])[:3, :3])
            bind[b][:3, 3] = (M @ rest[b])[:3, 3]
            basis[b] = np.linalg.inv(bind[b]) @ skin @ bind[b]        # relative to the bone's rest
            basis[b][~visible[:, b], :3, :3] *= 1e-4
        if breakable:  # extent of the idle loop (visible pieces), these Blender coordinates
            per_frame = {}
            for ob, b in members:
                co = np.empty(len(ob.data.vertices) * 3, np.float64)
                ob.data.vertices.foreach_get("co", co)
                co = co.reshape(-1, 3)
                for f in range(0, frames, 3):
                    if visible[f, b]:
                        k = M @ world[f, b] @ np.linalg.inv(rest[b]) @ M_inv
                        per_frame.setdefault(f, []).append(co @ k[:3, :3].T + k[:3, 3])
            per_frame = {f: np.concatenate(p) for f, p in per_frame.items()}
            pts = np.concatenate(list(per_frame.values()))
            f_low = min(per_frame, key=lambda f: per_frame[f][:, 2].min())  # the balloon at the bottom of its bob
            low = per_frame[f_low]
            bounds = (pts.min(0), pts.max(0), low.min(0), low.max(0))
            # the break object's static mesh (shown until hit), posed there; pop sprites left out
            copies = []
            for ob, b in members:
                if ob.get("hit_effect"):
                    continue
                cp = ob.copy()
                cp.data = ob.data.copy()
                bpy.context.scene.collection.objects.link(cp)
                k = M @ world[f_low, b] @ np.linalg.inv(rest[b]) @ M_inv
                co = np.empty(len(cp.data.vertices) * 3, np.float64)
                cp.data.vertices.foreach_get("co", co)
                cp.data.vertices.foreach_set("co", (co.reshape(-1, 3) @ k[:3, :3].T + k[:3, 3]).astype(np.float32).ravel())
                cp.data.update()
                copies.append(cp)
            export(copies, os.path.join(out_dir, f"{name}_{label}_Static.fbx"))
            for cp in copies:
                data = cp.data
                bpy.data.objects.remove(cp, do_unlink=True)
                bpy.data.meshes.remove(data)
        bpy.ops.object.select_all(action="DESELECT")
        for ob, b in members:
            vg = ob.vertex_groups.new(name=f"b{b}")
            vg.add(list(range(len(ob.data.vertices))), 1.0, "REPLACE")
            ob.select_set(True)
        bpy.context.view_layer.objects.active = members[0][0]
        if len(members) > 1:
            bpy.ops.object.join()
        mesh_ob = members[0][0]
        mesh_ob.name = mesh_ob.data.name = f"SK_{name}_{label}"
        arm_data = bpy.data.armatures.new(f"Arm_{label}")
        arm_ob = bpy.data.objects.new(f"Arm_{label}", arm_data)
        bpy.context.scene.collection.objects.link(arm_ob)
        bpy.ops.object.select_all(action="DESELECT")
        arm_ob.select_set(True)
        bpy.context.view_layer.objects.active = arm_ob
        bpy.ops.object.mode_set(mode="EDIT")
        for b in bones:
            eb = arm_data.edit_bones.new(f"b{b}")
            # give the bone a length first: on a zero-length bone Blender drops most of the assigned
            # orientation (rotation keys then turned pieces about the wrong axes -- the sun's face rolled)
            eb.head, eb.tail = (0.0, 0.0, 0.0), (0.0, 0.5, 0.0)
            eb.matrix = mathutils.Matrix(bind[b].tolist())
        bpy.ops.object.mode_set(mode="OBJECT")
        mesh_ob.parent = arm_ob
        mod = mesh_ob.modifiers.new("Armature", "ARMATURE")
        mod.object = arm_ob
        arm_ob.animation_data_create()
        action = write_action(arm_ob, f"A_{label}", bones, basis, frames)
        path = os.path.join(out_dir, f"{name}_{label}.fbx")
        export_skinned(arm_ob, mesh_ob, path, frames)
        if breakable:
            # ASR's pop (type-1 "Hit": balloon hidden from frame 4, burst sprite frames 4-16, flash 2-12),
            # cut before its last frames (the burst toggles back on at 280), as <label>_Hit.fbx on the same
            # skeleton; full decode (scale + visibility) on the idle loop's rest pose
            hit = next(a for a in tree["animations"] if a["name"] == "Hit")
            _, world_h, visible_h = branch_worlds(tree, hit, motion_only=False)
            world_h, visible_h = (L @ world_h)[:HIT_FRAMES], visible_h[:HIT_FRAMES]
            basis_h = {}
            for b in bones:
                skin = M @ world_h[:, b] @ np.linalg.inv(rest[b]) @ M_inv
                basis_h[b] = np.linalg.inv(bind[b]) @ skin @ bind[b]
                basis_h[b][~visible_h[:, b], :3, :3] *= 1e-4
            action_h = write_action(arm_ob, f"A_{label}_Hit", bones, basis_h, len(world_h))
            arm_ob.animation_data.action = action_h
            export_skinned(arm_ob, mesh_ob, os.path.join(out_dir, f"{name}_{label}_Hit.fbx"), len(world_h))
            bpy.data.actions.remove(action_h)
            arm_ob.animation_data.action = action
            json.dump({"min": bounds[0].tolist(), "max": bounds[1].tolist(),
                       "low_min": bounds[2].tolist(), "low_max": bounds[3].tolist()},
                      open(os.path.join(out_dir, f"{name}_{label}.bounds.json"), "w"))
        print(f"wrote {path} ({len(bones)} bones, {frames} frames, {len(members)} pieces)")
        exported.add(key)
        # Free everything right away: state left over from many exports crashed Blender's exporter.
        mesh_data = mesh_ob.data
        bpy.data.objects.remove(mesh_ob, do_unlink=True)
        bpy.data.objects.remove(arm_ob, do_unlink=True)
        bpy.data.meshes.remove(mesh_data)
        bpy.data.armatures.remove(arm_data)
        bpy.data.actions.remove(action)
    return exported


for old in (glob.glob(os.path.join(out_dir, f"{name}_Anim_t*.fbx")) + glob.glob(os.path.join(out_dir, f"{name}_Break_t*.fbx"))
            + glob.glob(os.path.join(out_dir, f"{name}_Anim_t*.bounds.json"))):
    os.remove(old)
animated = export_animated_trees()
print(f"animated trees exported: {sorted(animated)}")

# Join per material (and forest): a few hundred static meshes instead of ~2800 tiny ones.
by_mat = {}
for ob in bpy.data.objects:
    forest = "env_" if ob.name.startswith("envmap") else ""
    key = forest + (ob.data.materials[0].name if ob.data.materials and ob.data.materials[0] else "none")
    by_mat.setdefault(key, []).append(ob)
joined = []
for mat, obs in by_mat.items():
    bpy.ops.object.select_all(action="DESELECT")
    for ob in obs:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = obs[0]
    if len(obs) > 1:
        bpy.ops.object.join()
    obs[0].name = obs[0].data.name = f"SM_{name}_{mat}"[:60]
    joined.append(obs[0])
export(joined, os.path.join(out_dir, f"{name}_Visual.fbx"))

# Reference collision meshes for checking placement inside the editor.
for ref in refs:
    col = parse_collision(open(ref, "rb").read())
    ref_name = "REF_" + os.path.basename(os.path.dirname(ref)) + "_" + os.path.splitext(os.path.basename(ref))[0]
    objs = []
    for ci, ch in enumerate(col.chunks):
        verts = cw_to_blender(ch.vertices.astype(np.float64))
        for pi, part in enumerate(ch.parts):
            flags = struct.unpack_from("<I", ch.materials[part.material], 0)[0]
            if flags & 0x02000000 or flags & 0x20:   # skip simplified layer and respawn shell
                continue
            me = bpy.data.meshes.new(f"{ref_name}_c{ci}_p{pi}")
            me.from_pydata(verts.tolist(), [], part.triangles.tolist())
            ob = bpy.data.objects.new(me.name, me)
            bpy.context.scene.collection.objects.link(ob)
            objs.append(ob)
    bpy.ops.object.select_all(action="DESELECT")
    for ob in objs:
        ob.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    bpy.ops.object.join()
    objs[0].name = ref_name
    export([objs[0]], os.path.join(out_dir, f"{ref_name}.fbx"))
