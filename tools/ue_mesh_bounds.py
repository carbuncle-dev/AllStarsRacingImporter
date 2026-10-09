"""Unreal Editor Python: print bounding-box sizes of CrossWorlds machine meshes (for ASR->CW scale).

    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript=tools/ue_mesh_bounds.py -nullrhi
"""

import unreal

reg = unreal.AssetRegistryHelpers.get_asset_registry()
assets = reg.get_assets_by_path("/Game/01_Union/Asset/Machine", recursive=True)
for a in assets:
    name = str(a.asset_name)
    if not name.startswith("SK_") or "PhysicsAsset" in name or "Skeleton" in name or not name.endswith("_All"):
        continue
    mesh = unreal.load_asset(str(a.package_name))
    if not isinstance(mesh, unreal.SkeletalMesh):
        continue
    b = mesh.get_bounds()
    e = b.box_extent
    unreal.log(f"[BOUNDS] {name} size X {2 * e.x:.1f} Y {2 * e.y:.1f} Z {2 * e.z:.1f} cm")
unreal.log("[BOUNDS] BOUNDS_OK")
unreal.SystemLibrary.quit_editor()
