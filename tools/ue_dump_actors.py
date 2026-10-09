"""Unreal Editor Python: dump actor transforms of stage levels to JSON.

    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript=tools/ue_dump_actors.py -nullrhi
    (UE_DUMP_CONFIG env var -> {"levels": [...], "out": "actors.json"})
"""

import json
import os

import unreal

cfg = json.load(open(os.environ["UE_DUMP_CONFIG"]))
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
SKIP = {"WorldSettings", "Brush", "DefaultPhysicsVolume", "GameplayDebuggerPlayerManager", "AbstractNavData",
        "WorldDataLayers", "LevelBounds", "InstancedFoliageActor"}
out = {}
for level in cfg["levels"]:
    if not les.load_level(level):
        unreal.log_error(f"[DUMP] failed to load {level}")
        continue
    rows = []
    for a in eas.get_all_level_actors():
        cls = a.get_class().get_name()
        if cls in SKIP:
            continue
        l, r, s = a.get_actor_location(), a.get_actor_rotation(), a.get_actor_scale3d()
        rows.append({"name": a.get_name(), "label": a.get_actor_label(), "class": cls,
                     "location": [l.x, l.y, l.z], "rotation": [r.pitch, r.yaw, r.roll],
                     "scale": [s.x, s.y, s.z],
                     "attached": a.get_attach_parent_actor() is not None})
    out[level] = rows
    unreal.log(f"[DUMP] {level}: {len(rows)} actors")
json.dump(out, open(cfg["out"], "w"), indent=1)
unreal.SystemLibrary.quit_editor()
