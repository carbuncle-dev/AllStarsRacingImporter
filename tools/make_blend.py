"""Headless Blender: blender -b -P tools/make_blend.py -- <in.obj> <out.blend>

Imports a forest_to_obj export and saves an organised .blend: one collection
per Forest (track, envmap, item, track_shadow, cameras), with the non-visual
helper forests hidden. Textures are referenced relative to the .blend.
"""

import os
import sys

import bpy
import numpy as np

obj_path, blend_path = (os.path.abspath(a) for a in sys.argv[sys.argv.index("--") + 1:][:2])

# envmap holds the duplicate scenery the game renders into reflection maps.
HIDDEN = {"cameras", "item", "track_shadow", "envmap"}

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.wm.obj_import(filepath=obj_path)

scene = bpy.context.scene
collections = {}
for ob in list(scene.collection.objects):
    forest = ob.name.split(".Forest")[0] if ".Forest" in ob.name else "other"
    coll = collections.get(forest)
    if coll is None:
        coll = collections[forest] = bpy.data.collections.new(forest)
        scene.collection.children.link(coll)
    coll.objects.link(ob)
    scene.collection.objects.unlink(ob)

view_layer = bpy.context.view_layer
for name, coll in collections.items():
    if name in HIDDEN:
        view_layer.layer_collection.children[coll.name].hide_viewport = True
        coll.hide_render = True


def has_transparency(image) -> bool:
    if image.channels < 4 or not image.has_data and image.size[0] == 0:
        return False
    pixels = np.empty(image.size[0] * image.size[1] * 4, dtype=np.float32)
    image.pixels.foreach_get(pixels)
    return bool((pixels[3::4] < 0.98).any())


# The OBJ importer ignores texture alpha, so cut-outs (clouds, foliage, glasses)
# render black. Wire alpha into the BSDF for every texture that actually uses it.
alpha_cache = {}
for mat in bpy.data.materials:
    # Sky domes face inward; culling back faces lets you orbit the track from outside.
    mat.use_backface_culling = True
    if not mat.use_nodes:
        continue
    nodes, links = mat.node_tree.nodes, mat.node_tree.links
    bsdf = next((n for n in nodes if n.type == "BSDF_PRINCIPLED"), None)
    tex = next((l.from_node for l in bsdf.inputs["Base Color"].links), None) if bsdf else None
    if tex is None or tex.type != "TEX_IMAGE" or tex.image is None:
        continue
    img = tex.image
    if img.name not in alpha_cache:
        alpha_cache[img.name] = has_transparency(img)
    if not alpha_cache[img.name]:
        continue
    img.alpha_mode = "CHANNEL_PACKED"
    links.new(tex.outputs["Alpha"], bsdf.inputs["Alpha"])
    if hasattr(mat, "surface_render_method"):
        mat.surface_render_method = "DITHERED"
    else:
        mat.blend_method = "HASHED"
print(f"alpha wired for {sum(alpha_cache.values())}/{len(alpha_cache)} textures")

# Sky domes and cloud layers are closed shells many times larger than any track
# piece; in Cycles they would block the sun and world light entirely.
meshes = [ob for ob in scene.objects if ob.type == "MESH"]
extents = sorted(max(ob.dimensions) for ob in meshes)
shell_size = extents[int(len(extents) * 0.98)] * 2
for ob in meshes:
    if max(ob.dimensions) > shell_size:
        ob.visible_shadow = False

scene.world = bpy.data.worlds.new("Sky")
scene.world.color = (0.5, 0.6, 0.8)
sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", "SUN"))
sun.data.energy = 3
sun.rotation_euler = (0.6, 0.2, 0.5)
scene.collection.objects.link(sun)

# The track spans ~17 km in game units, beyond the default 1 km clip distance.
for screen in bpy.data.screens:
    for area in screen.areas:
        for space in area.spaces:
            if space.type == "VIEW_3D":
                space.clip_end = 100000
                space.shading.type = "MATERIAL"

bpy.ops.file.make_paths_relative()
bpy.ops.wm.save_as_mainfile(filepath=blend_path, relative_remap=True)
for name, coll in sorted(collections.items()):
    print(f"collection {name}: {len(coll.objects)} objects{' (hidden)' if name in HIDDEN else ''}")
