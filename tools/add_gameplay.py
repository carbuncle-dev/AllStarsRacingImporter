"""Headless Blender: blender -b <track.blend> -P tools/add_gameplay.py -- <gameplay.json>

Adds the exported collision and navigation data to an existing track .blend:
  collision/   one mesh per surface type, colour-coded (hidden by default)
  navigation/  racing line + safe line curves, road edge ribbon, waypoint path,
               and one empty per track marker grouped by marker type
Game space (Y up) is converted the same way Blender's OBJ importer does it.
"""

import json
import os
import sys

import bpy
from mathutils import Matrix, Vector

json_path = os.path.abspath(sys.argv[sys.argv.index("--") + 1])
data = json.load(open(json_path))
scene = bpy.context.scene

SURFACE_COLOURS = {
    "track_wooden_boards": (0.85, 0.55, 0.2),
    "track_metal": (0.6, 0.65, 0.75),
    "track_technometalmesh": (0.3, 0.45, 0.9),
    "track_tileswater": (0.1, 0.8, 0.9),
    "barrier_woodsplinters": (0.9, 0.15, 0.15),
    "reset": (0.8, 0.1, 0.8),
}


def to_blender(p):
    x, y, z = p
    return Vector((x, -z, y))


def new_collection(name, parent=scene.collection):
    old = bpy.data.collections.get(name)
    if old:
        for ob in list(old.all_objects):
            bpy.data.objects.remove(ob, do_unlink=True)
        for child in list(old.children):
            bpy.data.collections.remove(child)
        bpy.data.collections.remove(old)
    coll = bpy.data.collections.new(name)
    parent.children.link(coll)
    return coll


def colour_material(name, rgb):
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.diffuse_color = (*rgb, 1.0)
    mat.use_nodes = True
    mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (*rgb, 1.0)
    return mat


def polyline(name, points, coll, cyclic, rgb, bevel=0.0):
    curve = bpy.data.curves.new(name, "CURVE")
    curve.dimensions = "3D"
    curve.bevel_depth = bevel
    spline = curve.splines.new("POLY")
    spline.points.add(len(points) - 1)
    for pt, p in zip(spline.points, points):
        pt.co = (*to_blender(p), 1.0)
    spline.use_cyclic_u = cyclic
    curve.materials.append(colour_material(f"nav_{name}", rgb))
    ob = bpy.data.objects.new(name, curve)
    coll.objects.link(ob)
    return ob


# --- Collision --------------------------------------------------------------
col_coll = new_collection("collision")
verts = [to_blender(v) for v in data["collision"]["vertices"]]
for surface, info in data["collision"]["surfaces"].items():
    mesh = bpy.data.meshes.new(f"col_{surface}")
    used = sorted({i for tri in info["triangles"] for i in tri})
    remap = {old: new for new, old in enumerate(used)}
    # Game triangles are clockwise in a Y-up space; the axis swap keeps winding.
    mesh.from_pydata([verts[i] for i in used], [], [[remap[i] for i in t] for t in info["triangles"]])
    mesh.materials.append(colour_material(f"surface_{surface}", SURFACE_COLOURS.get(surface, (0.5, 0.5, 0.5))))
    ob = bpy.data.objects.new(f"col_{surface}", mesh)
    ob["surface_hash"] = f"{info['hash']:08x}"
    if surface == "reset":
        # Respawn shell wraps the whole course; wireframe keeps the road visible.
        ob.display_type = "WIRE"
    col_coll.objects.link(ob)
col_coll.hide_render = True
bpy.context.view_layer.layer_collection.children[col_coll.name].hide_viewport = True

# --- Navigation -------------------------------------------------------------
nav = data["navigation"]
nav_coll = new_collection("navigation")
nav_coll.hide_render = True

for i, rl in enumerate(nav["racing_lines"]):
    polyline(f"racing_line_{i}", rl["points"], nav_coll, rl["looping"], (0.1, 1.0, 0.2), bevel=0.8)
    polyline(f"safe_line_{i}", rl["safe_points"], nav_coll, rl["looping"], (1.0, 0.9, 0.1), bevel=0.4)

polyline("waypoints", [w["pos"] for w in nav["waypoints"]], nav_coll, True, (1, 1, 1))

# Road edges: one quad per waypoint link, spanning its left/right extents.
edge_verts, edge_faces = [], []
for link in nav["links"]:
    nxt = nav["links"][nav["waypoints"][link["to"]]["to_links"][0]] if nav["waypoints"][link["to"]]["to_links"] else None
    if nxt is None:
        continue
    base = len(edge_verts)
    edge_verts += [to_blender(link["left"]), to_blender(link["right"]),
                   to_blender(nxt["right"]), to_blender(nxt["left"])]
    edge_faces.append([base, base + 1, base + 2, base + 3])
mesh = bpy.data.meshes.new("road_bounds")
mesh.from_pydata(edge_verts, [], edge_faces)
mesh.materials.append(colour_material("nav_road_bounds", (0.2, 0.4, 1.0)))
ob = bpy.data.objects.new("road_bounds", mesh)
ob.display_type = "WIRE"
nav_coll.objects.link(ob)

marker_colls = {}
for i, m in enumerate(nav["track_markers"]):
    coll = marker_colls.get(m["type"])
    if coll is None:
        coll = marker_colls[m["type"]] = new_collection(f"markers_type_{m['type']:02d}", nav_coll)
    empty = bpy.data.objects.new(f"marker_t{m['type']:02d}_v{m['value']}_{i:02d}", None)
    empty.empty_display_type = "SINGLE_ARROW"
    empty.empty_display_size = max(m["radius"], 5.0)
    fwd, up = to_blender(m["dir"]), to_blender(m["up"])
    if fwd.length > 1e-4 and up.length > 1e-4:
        right = fwd.cross(up).normalized()
        up = right.cross(fwd).normalized()
        # Empty's arrow points along local Z: map Z -> forward, Y -> up.
        rot = Matrix((right, up, fwd.normalized())).transposed()
        empty.matrix_world = Matrix.Translation(to_blender(m["pos"])) @ rot.to_4x4()
    else:
        empty.location = to_blender(m["pos"])
    for key in ("type", "value", "radius", "waypoint", "track_dist", "linked_marker", "flags", "text"):
        empty[key] = m[key]
    coll.objects.link(empty)

bpy.ops.wm.save_mainfile()
print(f"added collision ({len(data['collision']['surfaces'])} surfaces) and navigation "
      f"({len(nav['waypoints'])} waypoints, {len(nav['track_markers'])} markers in {len(marker_colls)} types)")
