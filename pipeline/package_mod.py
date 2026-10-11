"""Package converted stage payloads into a CrossWorlds IoStore mod (replacing a stage's data assets).

    python -m pipeline.package_mod out/samba_Hard/cw --stage stg1901 --name RockyCoaster

Takes the template stage's legacy assets (from `retoc to-legacy`), swaps in the
new payloads, fixes the size fields, and converts to IoStore with retoc:
    out/mods/zzz_<name>_P.{utoc,ucas,pak}
Install by copying those three files to <CrossWorlds>/UNION/Content/Paks/~mods/.
"""

from __future__ import annotations

import argparse
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from cw.blob import extract_payload

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "extracted" / "cw" / "legacy"
RETOC = ROOT / "tools" / "bin" / "retoc.exe"
PAKS = Path(r"C:\Program Files (x86)\Steam\steamapps\common\SonicRacingCrossWorlds\UNION\Content\Paks")

# payload file in the converted dir -> asset name suffix
ASSETS = {
    "Lap_Collision.bin": "Collision",
    "FinalLap_Collision.bin": "FinalLap_Collision",
    "Lap_Navigation.bin": "Navigation",
    "FinalLap_Navigation.bin": "FinalLap_Navigation",
    "Lap_ComPath.bin": "ComPath",
    "FinalLap_ComPath.bin": "FinalLap_ComPath",
}


def find_asset(stage: str, suffix: str) -> Path:
    hits = list(LEGACY.rglob(f"{stage}_{suffix}.uasset"))
    if len(hits) != 1:
        raise FileNotFoundError(f"expected one legacy {stage}_{suffix}.uasset, found {hits}")
    return hits[0]


def patch_fields(prefix: bytes, payload: bytes, suffix: str) -> bytes:
    """Update the int32 fields serialized before the byte array."""
    prefix = bytearray(prefix)
    if suffix.endswith("Collision"):
        # Parameter{DataVersion, Hash, ChunkDataCount, Data}: last three int32 before the array.
        version, h, chunks = struct.unpack_from("<3I", payload, 4)
        struct.pack_into("<3I", prefix, len(prefix) - 12, version, h, chunks)
    elif suffix.endswith("Navigation"):
        struct.pack_into("<I", prefix, len(prefix) - 4, struct.unpack_from("<I", payload, 4)[0])
    return bytes(prefix)


def patch_asset(uasset: bytes, uexp_old: bytes, payload: bytes, suffix: str) -> tuple[bytes, bytes]:
    prefix, _ = extract_payload(uexp_old)
    uexp = patch_fields(prefix, payload, suffix) + struct.pack("<I", len(payload)) + payload + uexp_old[-8:]
    asset = bytearray(uasset)
    # Export SerialSize (uexp minus the package tag) and BulkDataStartOffset (both int64).
    for old, new in ((len(uexp_old) - 4, len(uexp) - 4),
                     (len(uasset) + len(uexp_old) - 4, len(uasset) + len(uexp) - 4)):
        pat = struct.pack("<q", old)
        hits = [i for i in range(len(asset) - 7) if asset[i:i + 8] == pat]
        if len(hits) != 1:
            raise ValueError(f"size field {old} found {len(hits)} times in uasset")
        struct.pack_into("<q", asset, hits[0], new)
    return bytes(asset), uexp


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("converted", type=Path, help="folder with Collision.bin, Lap_Navigation.bin, ...")
    ap.add_argument("--stage", default="stg1901")
    ap.add_argument("--name", default="RockyCoaster")
    ap.add_argument("--out", type=Path, default=ROOT / "out" / "mods")
    ap.add_argument("--extra", type=Path, help="folder of additional cooked legacy files (UNION/Content/...) to include")
    ap.add_argument("--new-stages", help="move the mod off --stage onto these new stage ids (e.g. 3001,3002,3003; "
                    "pipeline.relocate_stage): the stock stage stays untouched")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "legacy"
        for fname, suffix in ASSETS.items():
            src = args.converted / fname
            if not src.exists():
                print(f"skip {suffix}: {src.name} not found")
                continue
            ua = find_asset(args.stage, suffix)
            rel = ua.relative_to(LEGACY)
            asset, uexp = patch_asset(ua.read_bytes(), ua.with_suffix(".uexp").read_bytes(), src.read_bytes(), suffix)
            # Self-check: the patched export still yields exactly our payload.
            assert extract_payload(uexp)[1] == src.read_bytes()
            dst = staging / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(asset)
            dst.with_suffix(".uexp").write_bytes(uexp)
            print(f"patched {rel.as_posix()} ({len(uexp)} byte export)")

        if args.extra:
            files = [f for f in args.extra.rglob("*") if f.is_file()]
            for f in files:
                dst = staging / f.relative_to(args.extra)
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(f, dst)
            print(f"added {len(files)} cooked files from {args.extra}")

        if args.new_stages:
            from pipeline.relocate_stage import relocate
            copies = relocate(staging, args.stage, [int(x) for x in args.new_stages.split(",")], music=args.name)
            # the game streams only level packages its registry knows
            from pipeline.asset_registry import write_pak
            args.out.mkdir(parents=True, exist_ok=True)
            write_pak(copies, args.out / f"zzz_{args.name}_Registry_P.pak")

        args.out.mkdir(parents=True, exist_ok=True)
        utoc = args.out / f"zzz_{args.name}_P.utoc"
        subprocess.run([str(RETOC), "to-zen", "--version", "UE5_4", str(staging), str(utoc)], check=True)

        # Round trip through IoStore to confirm the container decodes back to our payloads.
        # retoc needs the game's global container (script objects) next to the mod to decode it.
        zen_dir, check = Path(tmp) / "zen", Path(tmp) / "check"
        zen_dir.mkdir()
        for f in [PAKS / "global.utoc", PAKS / "global.ucas"] + [utoc.with_suffix(e) for e in (".utoc", ".ucas", ".pak")]:
            shutil.copy(f, zen_dir / f.name)
        subprocess.run([str(RETOC), "to-legacy", "--version", "UE5_4", "--no-shaders", str(zen_dir), str(check)],
                       check=True, capture_output=True)
        for fname, suffix in ASSETS.items():
            src = args.converted / fname
            if src.exists():
                from pipeline.relocate_stage import CONTENT_PREFIX
                stage_name = f"{CONTENT_PREFIX}{args.new_stages.split(',')[0]}" if args.new_stages else args.stage
                got = next(check.rglob(f"{stage_name}_{suffix}.uexp"), None) or next(check.rglob(f"{args.stage}_{suffix}.uexp"))
                assert extract_payload(got.read_bytes())[1] == src.read_bytes(), f"{suffix} differs after IoStore round trip"
        print("IoStore round trip OK")

    for ext in (".utoc", ".ucas", ".pak"):
        f = utoc.with_suffix(ext)
        print(f"  {f}  ({f.stat().st_size if f.exists() else 'missing'} bytes)")
    print(f"Install: copy these files to {PAKS / '~mods'}")


if __name__ == "__main__":
    main()
