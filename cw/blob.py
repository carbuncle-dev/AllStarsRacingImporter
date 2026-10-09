"""Extract the binary payloads from CrossWorlds stage data assets.

The four stage data classes (from the WistfulHopes/UNION headers) each wrap one
TArray<uint8> with a few int32 fields in front, serialized as unversioned
properties:
    UUnionBtCollisionBinaryAsset    Parameter{DataVersion, Hash, ChunkDataCount, Data}
    UUnionNavigationBinaryDataAsset Parameter{DataVersion, Data}
    UUnionRunPathBinaryDataAsset    RawData
    UUnionComPathBinaryDataAsset    RawData
In the legacy .uexp the array is the last property: an int32 length followed by
the bytes, then 4 zero bytes and the package tag. We locate it by that invariant
instead of decoding the unversioned header.

    python -m cw.blob extracted/cw/legacy extracted/cw/blobs
"""

from __future__ import annotations

import argparse
import re
import struct
from pathlib import Path

PACKAGE_TAG = b"\xc1\x83\x2a\x9e"
TRAILER = 8  # u32 0 + package tag
KINDS = ("Collision", "Navigation", "ComPath_EditData", "ComPath", "RunPath")


def extract_payload(uexp: bytes) -> tuple[bytes, bytes]:
    """Return (fields before the array, array bytes)."""
    if not uexp.endswith(PACKAGE_TAG):
        raise ValueError("not a legacy .uexp (missing package tag)")
    for k in range(0, 64):
        n = struct.unpack_from("<I", uexp, k)[0]
        if k + 4 + n + TRAILER == len(uexp):
            return uexp[:k], uexp[k + 4:k + 4 + n]
    raise ValueError("byte array not found")


def classify(name: str) -> tuple[str, str, str] | None:
    """stg1001_FinalLap_ComPath -> ('stg1001', 'FinalLap', 'ComPath')."""
    m = re.match(r"(stg\d+(?:_Extnd\d+)?)_(FinalLap_)?(.+)$", name)
    if not m or m.group(3) not in KINDS:
        return None
    return m.group(1), "FinalLap" if m.group(2) else "Lap", m.group(3)


def main() -> None:
    ap = argparse.ArgumentParser(description="Extract stage data payloads to .bin files")
    ap.add_argument("legacy_root", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    count = 0
    for uexp in sorted(args.legacy_root.rglob("stg*.uexp")):
        info = classify(uexp.stem)
        if info is None:
            continue
        stage, lap, kind = info
        try:
            prefix, payload = extract_payload(uexp.read_bytes())
        except ValueError as e:
            print(f"skip {uexp.name}: {e}")
            continue
        (args.out / f"{stage}_{lap}_{kind}.bin").write_bytes(payload)
        count += 1
    print(f"extracted {count} payloads to {args.out}")


if __name__ == "__main__":
    main()
