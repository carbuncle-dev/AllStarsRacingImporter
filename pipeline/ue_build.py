"""Build, cook and package a stage's visual level with Ryn's custom UE 5.4 (headless).

    python -m pipeline.ue_build out/samba_Hard --stage stg1016 --name RockyCoaster [--steps build,cook,package]

build    runs tools/ue_build_level.py in the editor: imports <dir>/unreal/Samba_Hard_Visual.fbx,
         textures and materials into /Game/ASR/<name>, writes a new level at the stage's visual-level path
cook     cooks that level (and everything it references) for Windows, unversioned like the retail game
package  stages the cooked level + new assets, the game's object levels with the retargeted actors
         patched in (pipeline.patch_object_levels), and the converted stage data
         (pipeline.package_mod) and builds out/mods/zzz_<name>_P.{utoc,ucas,pak}
         --portal-height scales the stage's travel-ring HeightOffset (DT_TravelRingMainCourseData)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import os
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np

from pipeline import cloud_floor, sky_fill
from pipeline.package_mod import PAKS

ROOT = Path(__file__).resolve().parent.parent
# The UNION editor build (WistfulHopes/UNION project, UE 5.4); override with UE_ENGINE_DIR.
ENGINE = Path(os.environ.get("UE_ENGINE_DIR", Path.home() / "UE5-SRCW" / "Windows"))
EDITOR = ENGINE / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
PROJECT = ENGINE / "UNION" / "UNION.uproject"
COOKED = ENGINE / "UNION" / "Saved" / "Cooked" / "Windows"
LOGS = ROOT / "out" / "logs"
COMMON = ["-unattended", "-nosplash", "-nop4", "-nosound", "-stdout", "-FullStdOutLogOutput", "-NoLogTimes"]


def run_editor(args: list[str], log_name: str, env: dict | None = None) -> str:
    LOGS.mkdir(parents=True, exist_ok=True)
    log = LOGS / log_name
    # The editor sometimes dies while loading its modules (access violation in UnionRun right after the Steam
    # SDK loads, before any script runs; intermittent, 2026-10-11 -- the machine's random crashes): such a
    # start is retried, up to three times.
    for attempt in range(3):
        with open(log, "w", encoding="utf-8", errors="replace") as f:
            proc = subprocess.Popen([str(EDITOR), str(PROJECT), *args, *COMMON], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env={**os.environ, **(env or {})},
                                    text=True, encoding="utf-8", errors="replace")
            for line in proc.stdout:
                f.write(line)
            proc.wait()
        head = log.read_text(encoding="utf-8", errors="replace").split("=== Critical error: ===")
        if proc.returncode == 0 or len(head) < 2 or "LogPython: [" in head[0] or "Steam SDK Loaded!" not in head[0][-400:]:
            break
        print(f"{log_name}: the editor crashed while starting (attempt {attempt + 1}), retrying")
    text = log.read_text(encoding="utf-8", errors="replace")
    errors = [l for l in text.splitlines() if "Error:" in l and "LogWindows" not in l]
    print(f"{log_name}: exit {proc.returncode}, {len(errors)} error lines (log: {log})")
    for l in errors[:15]:
        print("   ", l.strip()[:200])
    if proc.returncode != 0:
        raise SystemExit(f"editor step failed, see {log}")
    return text


def level_path(stage: str) -> str:
    return f"/Game/01_Union/Map/Stage_NotNX/{stage}/SL_{stage}"


# The stage's object levels whose actors pipeline.retarget_actors moved onto the new track
# (travel ring, rings, dash panels, respawn points, goal). The game's cooked levels are patched
# in place by pipeline.patch_object_levels (see stage_object_levels) and shipped with the mod.
def object_levels(stage: str) -> list[str]:
    from pipeline.retarget_actors import EXTRA_LEVELS
    return [f"/Game/01_Union/Map/Stage/{stage}/SL_{stage}{suffix}"
            for suffix in ("_Object_Game", "_Object_Game_FinalLap", *EXTRA_LEVELS)]


def convert_textures(unreal_dir: Path) -> Path:
    """Re-save textures as PNG: UE's importer rejects most TGAs written by forest_to_obj."""
    from PIL import Image
    materials = json.loads((unreal_dir / "materials.json").read_text())
    out_dir = unreal_dir / "textures"
    out_dir.mkdir(exist_ok=True)
    converted = {}
    for info in materials.values():
        for key in ("texture", "ripple", "env", "layer2", "sparkle", "glow"):
            src = info.get(key)
            if not src:
                continue
            if src not in converted:
                dst = out_dir / (Path(src).stem + ".png")
                if not dst.exists():
                    Image.open(src).save(dst)
                converted[src] = str(dst.resolve())
            info[key] = converted[src]
    # export_for_unreal.close_cloud_floor's disc: one large non-repeating cloud-sea texture
    floor = cloud_floor.make(unreal_dir)
    materials["cloud_floor"] = {**materials.get("lambert15", {}), "kind": "opaque", "masked": False,
                                "texture": str(floor.resolve())}
    path = unreal_dir / "materials_png.json"
    path.write_text(json.dumps(materials, indent=1))
    print(f"converted {len(converted)} textures to PNG")
    return path


# Metal Harbor's race exposure: manual, bias 9 with the default physical camera (f/4, 1/60 s, ISO 100:
# EV100 = log2(4^2 * 60) = 9.91) -> 2^(9 - 9.91) / 1.2. Verified in game (sky colour = prediction).
EXPOSURE = 2 ** (9.0 - math.log2(4.0 ** 2 * 60.0)) / 1.2


def ensure_editor_plugin() -> None:
    """Install and compile tools/ue_plugin/AsrEditorTools (editor-only: Anim Blueprint authoring) in the
    editor project. Built alone (-Module): a full target build wants to rebuild every module, and UE 5.4's
    shared PCHs fail under MSVC 14.42. -Module builds write no module manifest: it is written here with the
    editor's BuildId."""
    src = ROOT / "tools" / "ue_plugin" / "AsrEditorTools"
    dst = ENGINE / "UNION" / "Plugins" / "AsrEditorTools"
    dll = dst / "Binaries" / "Win64" / "UnrealEditor-AsrEditorTools.dll"
    newest = max(p.stat().st_mtime for p in src.rglob("*") if p.is_file())
    if dll.exists() and dll.stat().st_mtime >= newest:
        return
    shutil.copytree(src, dst, dirs_exist_ok=True)
    build_bat = ENGINE / "Engine" / "Build" / "BatchFiles" / "Build.bat"
    log = LOGS / "plugin_build.log"
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(log, "w", encoding="utf-8", errors="replace") as f:
        proc = subprocess.run([str(build_bat), "UNIONEditor", "Win64", "Development", f"-Project={PROJECT}",
                               "-Module=AsrEditorTools", "-WaitMutex"], stdout=f, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise SystemExit(f"AsrEditorTools build failed, see {log}")
    build_id = json.loads((ENGINE / "UNION" / "Binaries" / "Win64" / "UnrealEditor.modules").read_text())["BuildId"]
    (dll.parent / "UnrealEditor.modules").write_text(json.dumps(
        {"BuildId": build_id, "Modules": {"AsrEditorTools": dll.name}}, indent="	"))
    print(f"built editor plugin AsrEditorTools ({log})")


def break_ground(track_dir: Path, stage: str) -> dict:
    """UE height of our road under each placed object (locator i), for the break objects' heights."""
    loc_path = next(track_dir.glob("*.locators.json"), None)
    if loc_path is None:
        return {}
    from cw.collision import parse_collision
    from pipeline.asr_to_cw import drivable_triangles
    from pipeline.retarget_actors import Route, _on_road
    conv = track_dir / f"cw_{stage}"
    a = json.loads((conv / "align.json").read_text())
    rot, off = np.array(a["rotation"]), np.array(a["offset"])
    road = drivable_triangles(parse_collision((conv / "Lap_Collision.bin").read_bytes()))
    route = Route((conv / "Lap_Navigation.bin").read_bytes(), road)
    return {str(i): float(_on_road(rot @ np.array(r["position"]) + off, route, 0.0)[0][2])
            for i, r in enumerate(json.loads(loc_path.read_text()))}


def build(track_dir: Path, stage: str, name: str) -> None:
    unreal_dir = track_dir / "unreal"
    cfg = {
        "fbx": str(next(unreal_dir.glob("*_Visual.fbx")).resolve()),
        "animated_fbx": [str(p.resolve()) for p in sorted(unreal_dir.glob("*_Anim_t*.fbx"))
                         if not p.stem.endswith(("_Hit", "_Static"))],
        # ASR's balloons as CrossWorlds break objects (export_for_unreal BREAKABLE_TREES): their pop animation
        # and idle-loop extent, by animated label
        "break_objects": {p.stem.split("_Anim_")[0] + "_Anim_" + p.stem.split("_Anim_")[1][:-len("_Hit")]: {
            "hit_fbx": str(p.resolve()),
            "static_fbx": str(p.with_name(p.stem[:-len("_Hit")] + "_Static.fbx").resolve()),
            "bounds": json.loads(p.with_name(p.stem[:-len("_Hit")] + ".bounds.json").read_text())}
            for p in sorted(unreal_dir.glob("*_Anim_t*_Hit.fbx"))},
        "break_ground": break_ground(track_dir, stage),
        # loop start of objects placed by ASR's logic (pipeline.asr_objects): "Anim_t<tree>_i<n>" -> fraction
        "anim_start": {f"Anim_t{r['tree']}_i{i}": r.get("start", 0.0) for i, r in enumerate(
            json.loads(next(track_dir.glob("*.locators.json")).read_text()))} if any(track_dir.glob("*.locators.json")) else {},
        "materials": str(convert_textures(unreal_dir).resolve()),
        "content_root": f"/Game/ASR/{name}",
        "level_path": level_path(stage),
        "unlit_match": ["skydome"],
        # ASR's baked vertex lighting, read from UV channels 1-2 (export_for_unreal); exact values.
        "vertex_lighting": True,
        # Lighting and exposure of a stock CrossWorlds day course, Metal Harbor (stg1022), read at runtime
        # with UE4SS (tools/ue4ss/RockyDump, out/runtime/dump_021940_1.txt): its post-process volumes use
        # MANUAL exposure (bias 8.5-9.5; ours was the engine's auto exposure, which made brightness swing
        # with the view and left the racers dark); sun 15 at 32.9 degrees, warm (253, 224, 179); sky light
        # 5 (here as channel-0 fill lights, pipeline.sky_fill: a sky light lit the travel-ring course too).
        # Bloom: Metal Harbor's (6, threshold -0.8: every pixel) hazed ASR's flat colours in game; only
        # what is brighter than ASR's white (> 1 after exposure: the glowing bulbs) blooms.
        # The post-process volume is UNBOUNDED: in game our bounded volume was never applied (exposure, bloom
        # and even a full desaturation test had no effect in race, while it works in the editor), whereas an
        # unbounded one demonstrably applies. To stay self-contained it has the lowest priority and only
        # sets what every travel-ring course's own volumes also set (stg2007, stg2010: bloom method /
        # intensity / threshold, exposure method / bias; runtime dump 2026-10-09), so theirs win in their
        # courses. Exposure: Metal Harbor's (manual, bias 9 with the default physical camera; the race
        # camera overrides nothing, so f/4: EXPOSURE). Bloom: only what is brighter than ASR's white.
        "lighting": {"post_process": {"auto_exposure_method": "AEM_MANUAL", "auto_exposure_bias": 9.0,
                                      "bloom_method": "BM_SOG", "bloom_intensity": 1.0, "bloom_threshold": 1.0},
                     "post_process_unbound": True, "post_process_priority": -100.0,
                     # Sun yaw: Metal Harbor's (30) shines from 210 deg, straight behind the racers on our grid
                     # (they face 0 deg): the intro cameras, which look at their faces, saw them dark. The
                     # CustomGrandPrix UE4SS mod turns our directional lights by 180 deg for the intro only
                     # (the scenery is unlit: they only light racers and items) and back for the race.
                     "sun": 15.0, "sun_color": [253 / 255, 224 / 255, 179 / 255], "sun_rotation": [-32.87, 30.0],
                     "sky_fill": sky_fill.fit(5.0)},
        # ASR's colours are final on-screen colours (lighting baked in), so the unlit scenery is scaled by
        # 1 / exposure: exposure x gain = 1 hands ASR's colours to the tonemapper unchanged. Manual
        # exposure = 2^(bias - EV100) / 1.2 with the default physical camera (f/4, 1/60 s, ISO 100:
        # EV100 = log2(4^2 * 60) = 9.91) -> 2^(9 - 9.91) / 1.2 = 0.445 at Metal Harbor's bias 9. (Scaling
        # them like albedo under the sun and sky, x5.8, rendered white.) Racers are lit by the course's
        # lights under the same exposure, as in Metal Harbor.
        "scenery_gain": round(1 / EXPOSURE, 3),
    }
    # Remove previous outputs on disk (deleting them from inside the editor crashes it).
    content = ENGINE / "UNION" / "Content"
    shutil.rmtree(content / "ASR" / name, ignore_errors=True)
    old_level = content / level_path(stage).replace("/Game/", "").replace("/", os.sep)
    for ext in (".umap", ".uexp"):
        Path(str(old_level) + ext).unlink(missing_ok=True)
    ensure_editor_plugin()
    cfg_path = unreal_dir / "build_config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    text = run_editor([f"-ExecutePythonScript={ROOT / 'tools' / 'ue_build_level.py'}", "-nullrhi"], "ue_build.log",
                      {"UE_BUILD_CONFIG": str(cfg_path.resolve())})
    for line in text.splitlines():
        if "[ASR]" in line:
            print("   ", line.split("[ASR]", 1)[1].strip())
    if "BUILD_OK" not in text:
        raise SystemExit("level build did not finish")


def stage_object_levels(track_dir: Path, stage: str, extra: Path) -> None:
    """Patch the moved actors into the game's original object levels (pipeline.patch_object_levels).

    Re-saving these levels in this editor dropped 1.4.1-only components (boost pad triggers), so they
    are never cooked here: the shipped cooked levels are extracted and only their transforms edited.
    """
    original = ROOT / "extracted" / "cw" / "objlevel"
    if not all(Path(str(original / "UNION" / "Content" / lv.replace("/Game/", ""))).with_suffix(".uexp").exists()
               for lv in object_levels(stage)):
        key = (Path(os.environ.get("TEMP", "")) / "cw_aes.txt").read_text().strip()
        for lv in object_levels(stage):
            subprocess.run([str(ROOT / "tools" / "bin" / "retoc.exe"), "-a", key, "to-legacy", "--version", "UE5_4",
                            "--no-shaders", "-f", lv.split("/")[-1], str(PAKS), str(original)], check=True)
    subprocess.run(["python", "-m", "pipeline.patch_object_levels",
                    str(track_dir / "unreal" / f"{stage}_actors_moved.json"), str(original), str(extra)],
                   check=True, cwd=ROOT)


def cook(stage: str) -> None:
    # Start from an empty cooked tree: iterative cooks re-evaluated the object levels' game
    # dependencies left from earlier runs and crashed recooking their textures.
    shutil.rmtree(COOKED, ignore_errors=True)
    # -cooksinglepackage: the level and what it references only; without it the cooker also picks up
    # startup/localized game assets (now present in the project) and crashes recooking their textures.
    text = run_editor(["-run=cook", "-targetplatform=Windows", "-cooksinglepackage", f"-map={level_path(stage)}", "-unversioned",
                "-skipeditorcontent", "-iterate"], "ue_cook.log")
    # A material that fails to compile cooks fine but renders as the engine's grey default in game.
    failed = sorted(set(re.findall(r"Failed to compile Material (/Game/\S+)", text)))
    if failed:
        raise SystemExit(f"materials failed to compile (grey in game): {failed}")


TRAVEL_RING_TABLE = "01_Union/Database/Stage/DataTable/DT_TravelRingMainCourseData"


def patch_travel_ring_height(extra: Path, stage: str, scale: float) -> None:
    """Stage a copy of the game's travel-ring course table with the stage row's HeightOffset scaled.

    The main-route portal is spawned by BP_TravelRingMainEnterSpawner at HeightOffset metres above it
    (10-12.5 m on retail stages). Rows are 24 bytes: FName row name (name index, number), then the
    unversioned struct -- u16 header 0x0700, u8 stage enum, f32 HeightOffset at row + 11, ... (the
    first row's enum is 0 and replaced by a zero-mask byte, header 0x0780, so the float sits there too).
    """
    src = ENGINE / "UNION" / "Content" / TRAVEL_RING_TABLE
    asset, uexp = Path(str(src) + ".uasset").read_bytes(), bytearray(Path(str(src) + ".uexp").read_bytes())
    key = stage.encode() + b"\0"
    at = asset.find(struct.pack("<i", len(key)) + key)
    if at < 0:
        raise SystemExit(f"{stage} not found in {TRAVEL_RING_TABLE}")
    # Name map entries: i32 length, chars + NUL, u32 hash. Its start is the offset the package
    # summary points at -- the first chain of entries that reaches the stage name.
    idx = None
    for start in range(at, 0, -1):
        if struct.pack("<i", start) not in asset[:start]:
            continue
        i, k = start, 0
        while i < at and 0 < struct.unpack_from("<i", asset, i)[0] < 256:
            i += 4 + struct.unpack_from("<i", asset, i)[0] + 4
            k += 1
        if i == at:
            idx = k
    if idx is None:
        raise SystemExit(f"could not locate the name map of {TRAVEL_RING_TABLE}")
    # The row array: i32 count, then count x 24-byte rows, each with the 0x07 header byte at +9.
    table = next(p + 4 for p in range(len(uexp) - 4)
                 if 0 < (n := struct.unpack_from("<i", uexp, p)[0]) and p + 4 + 24 * n <= len(uexp)
                 and all(uexp[p + 4 + 24 * k + 9] == 0x07 for k in range(n)))
    count = struct.unpack_from("<i", uexp, table - 4)[0]
    rows = [table + 24 * k for k in range(count) if struct.unpack_from("<2I", uexp, table + 24 * k) == (idx, 0)]
    if len(rows) != 1:
        raise SystemExit(f"expected one {stage} row in {TRAVEL_RING_TABLE}, found {len(rows)}")
    old = struct.unpack_from("<f", uexp, rows[0] + 11)[0]
    if not 1.0 <= old <= 50.0:
        raise SystemExit(f"unexpected HeightOffset {old} for {stage}; table layout changed?")
    struct.pack_into("<f", uexp, rows[0] + 11, old * scale)
    dst = extra / "UNION" / "Content" / TRAVEL_RING_TABLE
    dst.parent.mkdir(parents=True, exist_ok=True)
    Path(str(dst) + ".uasset").write_bytes(asset)
    Path(str(dst) + ".uexp").write_bytes(bytes(uexp))
    print(f"travel ring HeightOffset for {stage}: {old} -> {old * scale} m")


def course_map_square(track_dir: Path, stage: str, margin: float = 0.1):
    """(OrthoRate, centre (UE X, Y) cm) of the course-map square around the new track's collision."""
    from cw.collision import parse_collision
    verts = np.concatenate([c.vertices for c in parse_collision(
        (track_dir / f"cw_{stage}" / "Lap_Collision.bin").read_bytes()).chunks]).astype(np.float64)
    lo, hi = verts.min(0), verts.max(0)
    # CW (x, y, z) m -> UE (X, Y) cm = (z, -x) * 100
    ue_lo = np.array([lo[2], -hi[0]]) * 100.0
    ue_hi = np.array([hi[2], -lo[0]]) * 100.0
    return float((ue_hi - ue_lo).max() * (1.0 + margin)), (ue_lo + ue_hi) / 2


def patch_course_map(extra: Path, stage: str, track_dir: Path, margin: float = 0.1) -> None:
    """Stage DA_<stage> with its course-map squares (Normal and FinalLap) fitted to the new track.

    FStageCourseMapInfo = {CourseMapTexture, f32 OrthoRate, f32 Angle, FVector Offset}: a square of
    OrthoRate UE units centred at -Offset (E-Stadium: 70000 at -(4000, 14500), its course's centre).
    The game only lets racers collect rings / hit boost pads inside that square (in-game test:
    pads outside it never fired, a pad above the stadium's height range inside it did), so it must
    cover the whole new course.
    """
    from cw.collision import parse_collision

    rel = f"01_Union/Database/Stage/DataAsset/DA_{stage}"
    src = ENGINE / "UNION" / "Content" / rel
    asset, uexp = Path(str(src) + ".uasset").read_bytes(), bytearray(Path(str(src) + ".uexp").read_bytes())
    size, centre = course_map_square(track_dir, stage, margin)
    found = 0
    for o in range(len(uexp) - 32):
        ortho, angle = struct.unpack_from("<2f", uexp, o)
        ox, oy, oz = struct.unpack_from("<3d", uexp, o + 8)
        if 1000.0 <= ortho <= 1e6 and -360.0 <= angle <= 360.0 and float(angle).is_integer() \
                and abs(ox) < 1e6 and abs(oy) < 1e6 and abs(oz) < 1e6 and (ox, oy) != (0.0, 0.0):
            print(f"course map @{o:#x}: OrthoRate {ortho} -> {size:.0f}, Offset ({ox}, {oy}) -> "
                  f"({-centre[0]:.0f}, {-centre[1]:.0f})")
            struct.pack_into("<f", uexp, o, size)
            struct.pack_into("<2d", uexp, o + 8, -centre[0], -centre[1])
            found += 1
    if found == 0:
        raise SystemExit(f"no course-map info found in {rel}")
    dst = extra / "UNION" / "Content" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    Path(str(dst) + ".uasset").write_bytes(asset)
    Path(str(dst) + ".uexp").write_bytes(bytes(uexp))


def package(track_dir: Path, stage: str, name: str, portal_height: float = 1.0,
            add: list[Path] | None = None, new_stages: str | None = None) -> None:
    content = COOKED / "UNION" / "Content"
    level_dir = content / "01_Union" / "Map" / "Stage_NotNX" / stage
    asset_dir = content / "ASR" / name
    if not level_dir.exists() or not asset_dir.exists():
        raise SystemExit(f"cooked output missing under {content}")
    extra = ROOT / "out" / "cooked_extra"
    if extra.exists():
        shutil.rmtree(extra)
    for src in (level_dir, asset_dir):
        dst = extra / "UNION" / "Content" / src.relative_to(content)
        shutil.copytree(src, dst)
    stage_object_levels(track_dir, stage, extra)
    patch_course_map(extra, stage, track_dir)
    ui = ROOT / "out" / "ui_cooked"  # loading-screen / course-select pictures (pipeline.loading_screen)
    for folder in ([ui] if ui.exists() else []) + (add or []):  # extra cooked files (UNION/Content/...)
        shutil.copytree(folder, extra, dirs_exist_ok=True)
    if portal_height != 1.0:
        patch_travel_ring_height(extra, stage, portal_height)
    n = sum(1 for _ in extra.rglob("*.uasset")) + sum(1 for _ in extra.rglob("*.umap"))
    print(f"staged {n} cooked packages from {content}")
    cmd = ["python", "-m", "pipeline.package_mod", str(track_dir / f"cw_{stage}"), "--stage", stage,
           "--name", name, "--extra", str(extra)]
    if new_stages:
        cmd += ["--new-stages", new_stages]
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("track_dir", type=Path, help="e.g. out/samba_Hard (contains unreal/ and cw_<stage>/)")
    ap.add_argument("--stage", default="stg1016")
    ap.add_argument("--name", default="RockyCoaster")
    ap.add_argument("--steps", default="build,cook,package")
    ap.add_argument("--portal-height", type=float, default=1.0,
                    help="scale for the stage's travel-ring height above the road (0.5 = half as high)")
    ap.add_argument("--add", type=Path, action="append", help="folder of extra cooked files to include")
    ap.add_argument("--new-stages", help="ship the track as these new stage ids (custom Grand Prix, e.g. "
                    "3001,3002,3003) instead of replacing --stage (pipeline.relocate_stage)")
    args = ap.parse_args()
    steps = args.steps.split(",")
    if "build" in steps:
        build(args.track_dir, args.stage, args.name)
    if "cook" in steps:
        cook(args.stage)
    if "package" in steps:
        package(args.track_dir, args.stage, args.name, args.portal_height, args.add, args.new_stages)


if __name__ == "__main__":
    main()
