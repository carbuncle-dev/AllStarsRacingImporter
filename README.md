# AllStarsRacingImporter

Tools for porting tracks from Sonic & SEGA All-Stars Racing (2010, PC) to Sonic Racing CrossWorlds.
Reads from your own game install. No game assets are stored in this repo.

## Exporting an ASR track to Blender

Requirements: Python 3.11, Visual Studio 2022 (C++), CMake, vcpkg with `zlib:x64-windows`, Blender 4.2+.

1. Clone the reference tools into `third_party/`:
   `TomKannenberg/SASR-Track-Stuff` (CppSLib) and `TomKannenberg/xpac-Tool` (its `MAPPING.GC` resolves archive hashes to names).
   Apply `tools/cppslib_cli/forest_to_obj.patch` inside `third_party/SASR-Track-Stuff` (`git apply`): it exports only the
   most detailed LOD (all LODs stacked on top of each other otherwise) and the primitive's own UV set
   (`FOREST_TO_OBJ_ALL_LODS=1` restores the old behaviour; `FOREST_TO_OBJ_DEBUG_{UV,LOD,ATTR,TEX}=1` print diagnostics).
   It also writes ASR's baked vertex colours into the OBJ (`v x y z r g b`; 0.5 = neutral, ASR modulates by 2x) and a
   `<name>.materials.json` sidecar with each material's shader flags, layers, floats and textures.
2. Build the headless exporters:
   ```
   cmake -S tools/cppslib_cli -B build/cppslib -G "Visual Studio 17 2022" -A x64 -DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake
   cmake --build build/cppslib --config Release
   ```
   Copy `zlib1.dll` from vcpkg next to the built executables.
3. Extract the track from `Tracks.xpac`. `.zif`/`.zig` entries are written as `.sif`/`.sig`:
   ```
   python -m asr.xpac "<ASR>/Resource/Tracks.xpac" -p "*/samba_hard*" -o extracted/asr
   ```
4. Convert to OBJ with textures, then to an organised `.blend`:
   ```
   build/cppslib/Release/forest_to_obj.exe extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/Samba_Hard.obj
   blender -b -P tools/make_blend.py -- out/samba_Hard/Samba_Hard.obj out/samba_Hard/RockyCoaster.blend
   ```
   `tools/render_preview.py` renders quick top-down and angled previews without opening the Blender window.
5. Export the gameplay data (collision mesh with surface types, waypoints, racing line, track markers) and add it to the `.blend`:
   ```
   python -m pipeline.export_track extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/gameplay.json
   blender -b out/samba_Hard/RockyCoaster.blend -P tools/add_gameplay.py -- out/samba_Hard/gameplay.json
   ```
   This adds a `collision` collection (hidden by default, one colour per surface type) and a `navigation` collection
   (racing line, safe line, road bounds, and track markers grouped by type).

Track ids: `Samba_Hard` is Rocky Coaster.

## Extracting CrossWorlds stage data

Requires `retoc` (https://github.com/trumank/retoc, v0.1.5) in `tools/bin/` and the game's AES key (game version 1.4.1).

```
retoc -a <AES_KEY> to-legacy --version UE5_4 --no-shaders -f _Collision "<CrossWorlds>/UNION/Content/Paks" extracted/cw/legacy
#   repeat with -f _Navigation, -f _ComPath, -f _RunPath, -f StageDataAsset
python -m cw.blob extracted/cw/legacy extracted/cw/blobs
python -m pytest tests
```

Format notes live in the module docstrings. All three required formats round-trip byte for byte on every stage:

| Asset | Module | Status |
|---|---|---|
| Collision (`UUnionBtCollisionBinaryAsset`) | `cw/collision.py` | Fully decoded; hash is CRC32 of data after the 16-byte header |
| Navigation (`UUnionNavigationBinaryDataAsset`) | `cw/navigation.py` | Structure decoded; some waypoint fields kept as raw bytes |
| AI paths (`UUnionComPathBinaryDataAsset`) | `cw/compath.py` | Points and path lists decoded; junction/dash section kept raw |
| Run path (`UUnionRunPathBinaryDataAsset`) | � | Not decoded; optional (9 retail stages ship without one) |

## Converting an ASR track to CrossWorlds stage data

```
python -m pipeline.asr_to_cw extracted/asr/Resource/Tracks/Samba_Hard.sif out/samba_Hard/cw_stg1016 --template stg1016
python -m pipeline.package_mod out/samba_Hard/cw_stg1016 --stage stg1016 --name RockyCoaster
```

- `--template` is the stage being replaced (here stg1016 E-STADIUM); its assets must be extracted to
  `extracted/cw/legacy` first (`retoc ... to-legacy -f stg1016_`). `asr_to_cw` writes `{Lap,FinalLap}_Collision.bin`,
  `{Lap,FinalLap}_Navigation.bin` and `{Lap,FinalLap}_ComPath.bin` for whichever of those the stage has.
  The ComPath layout is copied from `--compath-format` (default stg1901, a single MAIN path).
  Geometry is copied 1:1 (both games are Y-up with matching road widths), then rotated about Y and moved so the
  ASR start line sits on the template stage's start line. Waypoints are resampled to CW's ~4.8-unit spacing and
  snapped onto the road; template markers (start/goal etc.) are retargeted by distance from the start line.
  The AI "in" and "out" lines come from ASR's racing line and safe line.
- `package_mod` swaps the payloads into the template stage's assets and builds `out/mods/zzz_<name>_P.{utoc,ucas,pak}`
  (verified by converting back). Copy those to `<CrossWorlds>/UNION/Content/Paks/~mods/` to test offline.
- `blender -b -P tools/view_cw_stage.py -- out/samba_Hard/cw_stg1016 out/samba_Hard/RockyCoaster_CW.blend` shows
  the converted collision, waypoints, AI lines and markers.

Stage names come from `DB_Course` + `Localization/Game/<lang>/Game.locres` (e.g. stg1001 OCEAN VIEW,
stg1901 TIME TRIAL).
