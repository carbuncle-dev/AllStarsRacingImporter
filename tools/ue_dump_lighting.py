"""Unreal Editor Python: dump a stage level's lighting setup (lights, sky light, fog, post-process volumes).

    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript=tools/ue_dump_lighting.py -nullrhi
    (UE_DUMP_CONFIG env var -> {"level": "/Game/...", "out": "lighting.json"})

Component properties are written as UE's text export (every non-default property), so settings such as
post-process exposure can be copied onto the actors of a rebuilt level.
"""

import json
import os

import unreal

cfg = json.load(open(os.environ["UE_DUMP_CONFIG"]))
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
KEEP = ("Light", "Fog", "PostProcess", "Sky", "Reflection", "Exposure", "Atmosphere", "Cloud")


out = {"level": cfg["level"], "actors": []}
if not les.load_level(cfg["level"]):
    unreal.log_error(f"[DUMP] failed to load {cfg['level']}")
else:
    for a in eas.get_all_level_actors():
        cls = a.get_class().get_name()
        if not any(k in cls for k in KEEP):
            continue
        row = {"name": a.get_name(), "label": a.get_actor_label(), "class": cls,
               "location": [a.get_actor_location().x, a.get_actor_location().y, a.get_actor_location().z],
               "rotation": [a.get_actor_rotation().pitch, a.get_actor_rotation().yaw, a.get_actor_rotation().roll],
               "components": {}}
        for comp in a.get_components_by_class(unreal.ActorComponent):
            props = {}
            for name in ("intensity", "light_color", "temperature", "use_temperature", "indirect_lighting_intensity",
                         "volumetric_scattering_intensity", "source_type", "cubemap", "source_cubemap_angle",
                         "real_time_capture", "lower_hemisphere_is_black", "lower_hemisphere_color",
                         "fog_density", "fog_height_falloff", "fog_inscattering_color", "fog_max_opacity",
                         "start_distance", "fog_cutoff_distance", "directional_inscattering_exponent",
                         "directional_inscattering_color", "intensity_units", "attenuation_radius",
                         "lighting_channels", "cast_shadows", "atmosphere_sun_light", "mobility"):
                try:
                    props[name] = str(comp.get_editor_property(name))
                except Exception:
                    pass
            row["components"][comp.get_name() + ":" + comp.get_class().get_name()] = props
        if cls == "PostProcessVolume":
            row["unbound"] = a.get_editor_property("unbound")
            row["priority"] = a.get_editor_property("priority")
            row["blend_weight"] = a.get_editor_property("blend_weight")
            row["settings"] = a.get_editor_property("settings").export_text()
        out["actors"].append(row)
    unreal.log(f"[DUMP] {len(out['actors'])} lighting actors")
json.dump(out, open(cfg["out"], "w"), indent=1)
unreal.SystemLibrary.quit_editor()
