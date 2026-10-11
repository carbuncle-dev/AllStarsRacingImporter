"""Unreal Editor Python: render views of a level to PNG (for comparing against reference footage).

    UnrealEditor-Cmd.exe UNION.uproject -ExecutePythonScript=tools/ue_capture.py -RenderOffscreen -unattended
    (UE_CAPTURE_CONFIG env var -> {"level": "/Game/...", "out_dir": "...", "width": 960, "height": 540,
                                   "views": [{"name": "start", "location": [x, y, z], "rotation": [pitch, yaw, roll]}],
                                   "post_process": {...optional unbound PostProcessSettings overrides...}})

Uses a SceneCapture2D (final colour, post-processed) per view, so lighting and post-process settings can
be tuned without an in-game test.
"""

import json
import os

import unreal

cfg = json.load(open(os.environ["UE_CAPTURE_CONFIG"]))
les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def log(msg):
    unreal.log(f"[CAPTURE] {msg}")


if not les.load_level(cfg["level"]):
    log(f"failed to load {cfg['level']}")
    unreal.SystemLibrary.quit_editor()
else:
    world = unreal.EditorLevelLibrary.get_editor_world()
    pp_cfg = cfg.get("post_process")
    if pp_cfg:
        ppv = eas.spawn_actor_from_class(unreal.PostProcessVolume, unreal.Vector(0, 0, 0))
        ppv.set_editor_property("unbound", True)
        ppv.set_editor_property("priority", 1000.0)  # above the level's own volumes
        pp = ppv.get_editor_property("settings")
        for prop, value in pp_cfg.items():
            if prop == "auto_exposure_method":
                value = getattr(unreal.AutoExposureMethod, value)
            elif isinstance(value, list) and len(value) == 4:  # e.g. color_saturation
                value = unreal.Vector4(*value)
            pp.set_editor_property("override_" + prop, True)
            pp.set_editor_property(prop, value)
        ppv.set_editor_property("settings", pp)
    # Optional: change settings on the level's own post-process volumes ("level_ppv": {prop: value}),
    # e.g. to test whether a camera is inside them.
    for actor in eas.get_all_level_actors() if cfg.get("level_ppv") else []:
        if isinstance(actor, unreal.PostProcessVolume) and not actor.get_editor_property("unbound"):
            pp = actor.get_editor_property("settings")
            for prop, value in cfg["level_ppv"].items():
                pp.set_editor_property("override_" + prop, True)
                pp.set_editor_property(prop, value)
            actor.set_editor_property("settings", pp)
            log(f"level volume {actor.get_actor_label()}: {cfg['level_ppv']}")
    # Optional: pose every animated tree at a given time (s) -- editor worlds don't play animations.
    if "anim_time" in cfg:
        for actor in eas.get_all_level_actors():
            if isinstance(actor, unreal.SkeletalMeshActor):
                comp = actor.skeletal_mesh_component
                data = comp.get_editor_property("animation_data")
                data.set_editor_property("saved_position", float(cfg["anim_time"]))
                data.set_editor_property("saved_playing", False)
                comp.set_editor_property("animation_data", data)
                anim = data.get_editor_property("anim_to_play")
                try:
                    if anim:
                        comp.play_animation(anim, False)
                        comp.set_position(float(cfg["anim_time"]), False)
                        comp.stop()
                except Exception as e:
                    log(f"posing {actor.get_actor_label()} failed: {e}")
        log(f"posed animated trees at {cfg['anim_time']} s")
    rt = unreal.RenderingLibrary.create_render_target2d(world, cfg.get("width", 960), cfg.get("height", 540),
                                                        getattr(unreal.TextureRenderTargetFormat,
                                                                cfg.get("rt_format", "RTF_RGBA8_SRGB")))
    cap = eas.spawn_actor_from_class(unreal.SceneCapture2D, unreal.Vector(0, 0, 0))
    comp = cap.capture_component2d
    comp.set_editor_property("texture_target", rt)
    # "capture_source": SCS_FINAL_COLOR_LDR (default) or e.g. SCS_SCENE_COLOR_HDR with "rt_format":
    # RTF_RGBA16F (linear scene colour, written as .hdr) to tonemap offline.
    comp.set_editor_property("capture_source", getattr(unreal.SceneCaptureSource,
                                                       cfg.get("capture_source", "SCS_FINAL_COLOR_LDR")))
    comp.set_editor_property("capture_every_frame", False)
    comp.set_editor_property("fov_angle", float(cfg.get("fov", 90.0)))
    # Scene captures ignore post-process volumes' exposure; "capture_post_process" sets the capture
    # component's own settings (e.g. a course's manual exposure) so renders match the game's exposure.
    cpp = cfg.get("capture_post_process")
    if cpp:
        pps = comp.get_editor_property("post_process_settings")
        for prop, value in cpp.items():
            if prop == "auto_exposure_method":
                value = getattr(unreal.AutoExposureMethod, value)
            elif prop == "bloom_method":
                value = getattr(unreal.BloomMethod, value)
            pps.set_editor_property("override_" + prop, True)
            pps.set_editor_property(prop, value)
        comp.set_editor_property("post_process_settings", pps)
        comp.set_editor_property("post_process_blend_weight", 1.0)
    os.makedirs(cfg["out_dir"], exist_ok=True)
    # Rendering only happens as the editor ticks: drive the views from a post-tick callback, waiting
    # frames for shader compilation and texture streaming before each capture.
    state = {"view": 0, "frames": 0, "handle": None}
    first_wait, view_wait = int(cfg.get("first_wait", 600)), int(cfg.get("view_wait", 30))

    def tick(_dt):
        views = cfg["views"]
        if state["frames"] == 0:
            log(f"tick: view {state['view']}/{len(views)}")
        if state["view"] >= len(views):
            unreal.unregister_slate_post_tick_callback(state["handle"])
            unreal.SystemLibrary.quit_editor()
            return
        view = views[state["view"]]
        if state["frames"] == 0:
            r = view["rotation"]
            if "fov" in view:
                comp.set_editor_property("fov_angle", float(view["fov"]))
            cap.set_actor_location_and_rotation(unreal.Vector(*view["location"]),
                                                unreal.Rotator(roll=r[2], pitch=r[0], yaw=r[1]), False, False)
        state["frames"] += 1
        comp.capture_scene()
        wait = first_wait if state["view"] == 0 else view_wait
        if state["frames"] >= wait:
            unreal.RenderingLibrary.export_render_target(world, rt, cfg["out_dir"], view["name"] + ("" if "16F" in cfg.get("rt_format", "") or "32F" in cfg.get("rt_format", "") else ".png"))
            log(f"wrote {view['name']}.png")
            state["view"] += 1
            state["frames"] = 0

    state["handle"] = unreal.register_slate_post_tick_callback(tick)
    log(f"registered tick callback for {len(cfg['views'])} views")
