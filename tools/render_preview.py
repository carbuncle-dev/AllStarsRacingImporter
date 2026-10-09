"""Headless Blender preview: blender -b -P tools/render_preview.py -- <in.obj> <out_prefix>

Imports a forest_to_obj export, keeps the track.Forest geometry and renders a
textured top-down and a three-quarter view with the Workbench engine.
"""

import os
import sys

import bpy
from mathutils import Vector

obj_path, out_prefix = (os.path.abspath(a) for a in sys.argv[sys.argv.index("--") + 1:][:2])

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.obj_import(filepath=obj_path)

for ob in list(bpy.data.objects):
    if not ob.name.startswith("track.Forest"):
        bpy.data.objects.remove(ob, do_unlink=True)

meshes = [ob for ob in bpy.data.objects if ob.type == "MESH"]
# Frame on the race area: ignore sky domes, cloud layers and ground planes,
# which are an order of magnitude larger than the individual track pieces.
extents = sorted(max(ob.dimensions) for ob in meshes)
limit = extents[int(len(extents) * 0.98)]
framed = [ob for ob in meshes if max(ob.dimensions) <= limit]
corners = [ob.matrix_world @ Vector(c) for ob in framed for c in ob.bound_box]
lo = Vector(min(c[i] for c in corners) for i in range(3))
hi = Vector(max(c[i] for c in corners) for i in range(3))
center, size = (lo + hi) / 2, hi - lo
print(f"bounds min={tuple(lo)} max={tuple(hi)}")

scene = bpy.context.scene
scene.render.engine = "BLENDER_WORKBENCH"
scene.display.shading.light = "STUDIO"
scene.display.shading.color_type = "TEXTURE"
# Sky domes face inward, so culling back faces makes them transparent from outside.
scene.display.shading.show_backface_culling = True
scene.render.resolution_x = scene.render.resolution_y = 1600
scene.world = bpy.data.worlds.new("World")
scene.world.color = (0.05, 0.05, 0.08)

cam_data = bpy.data.cameras.new("Cam")
cam_data.clip_end = max(size) * 10
cam = bpy.data.objects.new("Cam", cam_data)
scene.collection.objects.link(cam)
scene.camera = cam

# Top-down orthographic, Blender's OBJ importer maps the game's Y-up to Z-up.
cam_data.type = "ORTHO"
cam_data.ortho_scale = max(size.x, size.y) * 1.05
cam.location = center + Vector((0, 0, size.z + max(size)))
cam.rotation_euler = (0, 0, 0)
scene.render.filepath = f"{out_prefix}_top.png"
bpy.ops.render.render(write_still=True)

# Three-quarter perspective view.
cam_data.type = "PERSP"
cam_data.lens = 30
dist = max(size) * 0.9
cam.location = center + Vector((dist * 0.7, -dist * 0.7, dist * 0.6))
direction = center - cam.location
cam.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
scene.render.filepath = f"{out_prefix}_persp.png"
bpy.ops.render.render(write_still=True)
