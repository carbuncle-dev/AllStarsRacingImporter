"""Headless Blender: blender -b -P tools/view_cw_stage.py -- <stage_dir> <out.blend> [out.png]

Loads CrossWorlds payloads from <stage_dir> (Collision.bin, Lap_Navigation.bin,
ComPath.bin) into one scene: collision coloured by surface flags, the waypoint
loop, the AI in/out lines and the navigation markers (empties named by type).
"""

import os
import struct
import sys

import bpy
from mathutils import Matrix, Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cw.collision import parse_collision  # noqa: E402
from cw.compath import parse_compath  # noqa: E402
from cw.navigation import parse_navigation  # noqa: E402

args = [os.path.abspath(a) for a in sys.argv[sys.argv.index("--") + 1:]]
stage_dir, blend_path = args[0], args[1]
png_path = args[2] if len(args) > 2 else None

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def bl(p):
    """Game (x, y, z) Y-up -> Blender (x, -z, y)."""
    return Vector((float(p[0]), -float(p[2]), float(p[1])))


def material(name, rgb):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1)
    return mat


def collection(name, hide_render=False):
    c = bpy.data.collections.new(name)
    scene.collection.children.link(c)
    c.hide_render = hide_render
    return c


def polyline(name, points, coll, rgb, cyclic=True, bevel=0.6):
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = bevel
    sp = cu.splines.new("POLY")
    sp.points.add(len(points) - 1)
    for pt, p in zip(sp.points, points):
        pt.co = (*bl(p), 1)
    sp.use_cyclic_u = cyclic
    cu.materials.append(material(name, rgb))
    ob = bpy.data.objects.new(name, cu)
    coll.objects.link(ob)


KINDS = [(0x02000000, "layer2", (0.5, 0.5, 0.5)), (0x20, "respawn", (0.6, 0.2, 0.8)),
         (0x4, "wall", (0.85, 0.2, 0.2)), (0x1, "road", (0.25, 0.7, 0.35))]

col = parse_collision(open(os.path.join(stage_dir, "Lap_Collision.bin"), "rb").read())
cc = collection("collision")
for ci, ch in enumerate(col.chunks):
    verts = [bl(v) for v in ch.vertices]
    for pi, part in enumerate(ch.parts):
        flags = struct.unpack_from("<I", ch.materials[part.material], 0)[0]
        kind, rgb = next(((k, c) for bit, k, c in KINDS if flags & bit), ("other", (0.9, 0.8, 0.2)))
        mesh = bpy.data.meshes.new(f"c{ci}_p{pi}_{kind}")
        mesh.from_pydata(verts, [], part.triangles.tolist())
        mesh.materials.append(material(kind, rgb))
        ob = bpy.data.objects.new(mesh.name, mesh)
        ob["flags"] = f"{flags:08x}"
        cc.objects.link(ob)
        if kind in ("layer2", "respawn"):
            ob.hide_render = True
            ob.hide_set(True)

nc = collection("navigation", hide_render=False)
nav_path = os.path.join(stage_dir, "Lap_Navigation.bin")
if os.path.exists(nav_path):
    nav = parse_navigation(open(nav_path, "rb").read())
    order = sorted(range(len(nav.waypoints)), key=lambda i: struct.unpack_from("<I", nav.waypoints[i].raw, 0xEC)[0])
    polyline("waypoints", [nav.waypoints[i].pos + nav.waypoints[i].up * 0.5 for i in order], nc, (1, 1, 1), bevel=0.4)
    mc = bpy.data.collections.new("markers")
    nc.children.link(mc)
    for k, m in enumerate(nav.markers):
        e = bpy.data.objects.new(f"marker_t{m.type:02d}_{k:02d}", None)
        e.empty_display_type = "SINGLE_ARROW"
        e.empty_display_size = 8
        d = bl(struct.unpack_from("<3f", m.raw, 0x10))
        u = bl(struct.unpack_from("<3f", m.raw, 0x20))
        if d.length > 1e-4 and u.length > 1e-4:
            r = d.cross(u).normalized()
            rot = Matrix((r, r.cross(d.normalized()).normalized(), d.normalized())).transposed()
            e.matrix_world = Matrix.Translation(bl(m.pos)) @ rot.to_4x4()
        else:
            e.location = bl(m.pos)
        e["type"] = m.type
        mc.objects.link(e)

cp_path = os.path.join(stage_dir, "Lap_ComPath.bin")
if os.path.exists(cp_path):
    cp = parse_compath(open(cp_path, "rb").read())
    start = 0
    for p in cp.paths:
        n_in, n_out = len(p.in_distance), len(p.out_distance)
        pts = cp.points["pos"]
        polyline(f"ai_{p.name}_in", pts[start:start + n_in] + cp.points["up"][start:start + n_in] * 1.0, nc, (0.1, 0.6, 1.0))
        start += n_in
        if p.out_line != p.in_line:
            polyline(f"ai_{p.name}_out", pts[start:start + n_out] + cp.points["up"][start:start + n_out] * 1.0, nc, (1.0, 0.6, 0.1))
            start += n_out

for screen in bpy.data.screens:
    for area in screen.areas:
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.clip_end = 100000
bpy.ops.wm.save_as_mainfile(filepath=blend_path)

if png_path:
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = "MATERIAL"
    scene.render.resolution_x = scene.render.resolution_y = 1600
    cs = [ob.matrix_world @ Vector(c) for ob in cc.objects if not ob.hide_render for c in ob.bound_box]
    lo = Vector([min(c[i] for c in cs) for i in range(3)])
    hi = Vector([max(c[i] for c in cs) for i in range(3)])
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.data.type = "ORTHO"
    cam.data.clip_end = 100000
    cam.data.ortho_scale = max(hi.x - lo.x, hi.y - lo.y) * 1.05
    cam.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z + 1000)
    scene.render.filepath = png_path
    bpy.ops.render.render(write_still=True)
