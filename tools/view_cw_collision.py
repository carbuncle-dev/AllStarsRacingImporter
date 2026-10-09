"""Headless Blender: blender -b -P tools/view_cw_collision.py -- <out.png|out.blend> <Collision.bin> [<Collision.bin> ...]

Set OVERLAY=1 to draw all inputs in place (first in normal colours, the rest blue) instead of side by side.

Loads CrossWorlds collision payloads as meshes coloured by surface flags
(road green, wall red, respawn purple wireframe, 0x02000000 layer hidden),
laying multiple inputs side by side along X. Writes a top-down render (.png)
or a .blend for inspection.
"""

import os
import struct
import sys

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cw.collision import parse_collision  # noqa: E402

args = sys.argv[sys.argv.index("--") + 1:]
out_path, inputs = os.path.abspath(args[0]), [os.path.abspath(a) for a in args[1:]]

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene


def material(name, rgb):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1)
    return mat


def classify(flags):
    if flags & 0x02000000:
        return "layer2", (0.5, 0.5, 0.5)
    if flags & 0x20:
        return "respawn", (0.6, 0.2, 0.8)
    if flags & 0x4:
        return "wall", (0.85, 0.2, 0.2)
    if flags & 0x1:
        return "road", (0.2, 0.75, 0.3)
    return "other", (0.9, 0.8, 0.2)


offset_x = 0.0
for path in inputs:
    col = parse_collision(open(path, "rb").read())
    name = os.path.basename(os.path.dirname(path)) if os.path.basename(path) == "Collision.bin" else os.path.basename(path)
    lo, hi = Vector((1e9,) * 3), Vector((-1e9,) * 3)
    objs = []
    for ci, ch in enumerate(col.chunks):
        # Game (x, y, z) Y-up -> Blender (x, -z, y), as the OBJ importer does.
        verts = [(float(x), float(-z), float(y)) for x, y, z in ch.vertices]
        for pi, part in enumerate(ch.parts):
            flags = struct.unpack_from("<I", ch.materials[part.material], 0)[0]
            kind, rgb = classify(flags)
            if os.environ.get("OVERLAY") and path != inputs[0]:
                kind, rgb = kind + "_original", (0.25, 0.35, 0.9)
            mesh = bpy.data.meshes.new(f"{name}_c{ci}_p{pi}_{kind}")
            mesh.from_pydata(verts, [], part.triangles.tolist())
            mesh.materials.append(material(kind, rgb))
            ob = bpy.data.objects.new(mesh.name, mesh)
            ob["flags"] = f"{flags:08x}"
            scene.collection.objects.link(ob)
            if kind == "respawn":
                ob.display_type = "WIRE"
            if kind in ("layer2", "respawn"):
                ob.hide_render = True
            objs.append(ob)
            for v in verts:
                lo = Vector(map(min, lo, v))
                hi = Vector(map(max, hi, v))
    shift = 0 if os.environ.get("OVERLAY") else offset_x - lo.x
    for ob in objs:
        ob.location.x += shift
    print(f"{name}: extent {tuple(round(c) for c in (hi - lo))}")
    offset_x += 0 if os.environ.get("OVERLAY") else (hi.x - lo.x) + 100

if out_path.endswith(".blend"):
    bpy.ops.wm.save_as_mainfile(filepath=out_path)
else:
    bpy.context.view_layer.update()  # refresh matrix_world after the X shifts
    scene.render.engine = "BLENDER_WORKBENCH"
    scene.display.shading.color_type = "MATERIAL"
    scene.render.resolution_x, scene.render.resolution_y = 2000, 1200
    cs = [ob.matrix_world @ Vector(c) for ob in scene.objects if not ob.hide_render for c in ob.bound_box]
    lo = Vector([min(c[i] for c in cs) for i in range(3)])
    hi = Vector([max(c[i] for c in cs) for i in range(3)])
    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scene.collection.objects.link(cam)
    scene.camera = cam
    cam.data.type = "ORTHO"
    cam.data.clip_end = 100000
    cam.data.ortho_scale = max(hi.x - lo.x, (hi.y - lo.y) * 2000 / 1200) * 1.05
    cam.location = ((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, hi.z + 1000)
    scene.render.filepath = out_path
    bpy.ops.render.render(write_still=True)
