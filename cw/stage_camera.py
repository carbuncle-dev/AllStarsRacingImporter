"""CrossWorlds per-stage cutscene camera tables (Scene/Common/StageCameraSettings/DT_StageCameraSettings_*).

Rows are FStageCameraSettings (UNION headers): StageId (EStageId, uint8), BloomIntensity, BloomThreshold,
AutoExposureBias, CameraShutterSpeed, CurrentAperture (floats), AutoExposureMethod (uint8). Cooked with
unversioned properties: per row an FName (8 bytes), then u16 fragment headers (SkipNum:7, HasZeroes:1,
IsLast:1, ValueNum:7) and, if any property is zero, a zero mask (1 byte for <= 8 properties); zero
properties are not stored. The rows follow an i32 count; the table ends with the export's trailer.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

FIELDS = [("StageId", "B"), ("BloomIntensity", "f"), ("BloomThreshold", "f"), ("AutoExposureBias", "f"),
          ("CameraShutterSpeed", "f"), ("CurrentAperture", "f"), ("AutoExposureMethod", "B")]


@dataclass
class Row:
    name: bytes          # FName (index, number)
    values: dict         # field -> value (zero for unstored properties)
    offsets: dict        # field -> byte offset of the stored value (absent if stored as zero)


def find_rows(uexp: bytes) -> tuple[int, int]:
    """(offset of the row count, count): the first i32 followed by a parseable row sequence."""
    for o in range(0, 64):
        n = struct.unpack_from("<i", uexp, o)[0]
        if 1 <= n <= 200:
            try:
                rows = _parse(uexp, o + 4, n)
            except (struct.error, ValueError):
                continue
            if all(r.values["StageId"] < 200 for r in rows) and len({r.values["StageId"] for r in rows}) == n:
                return o, n
    raise ValueError("row table not found")


def _parse(d: bytes, o: int, n: int) -> list[Row]:
    rows = []
    for _ in range(n):
        name = d[o:o + 8]
        o += 8
        frags = []
        while True:
            h = struct.unpack_from("<H", d, o)[0]
            o += 2
            frags.append((h & 0x7F, bool(h & 0x80), (h >> 9) & 0x7F))
            if h & 0x100:
                break
        total = sum(skip + num for skip, _, num in frags)
        if total > len(FIELDS):
            raise ValueError("unexpected property count")
        zero_mask = 0
        if any(z for _, z, _ in frags):
            nvals = sum(num for _, _, num in frags)
            nbytes = 1 if nvals <= 8 else 2 if nvals <= 16 else 4
            zero_mask = int.from_bytes(d[o:o + nbytes], "little")
            o += nbytes
        values = {f: 0 for f, _ in FIELDS}
        offsets = {}
        idx, vi = 0, 0
        for skip, _, num in frags:
            idx += skip
            for _ in range(num):
                field, fmt = FIELDS[idx]
                if not (zero_mask >> vi) & 1:
                    values[field] = struct.unpack_from("<" + fmt, d, o)[0]
                    offsets[field] = o
                    o += struct.calcsize(fmt)
                idx += 1
                vi += 1
        rows.append(Row(name, values, offsets))
    return rows


def read(uexp: bytes) -> list[Row]:
    o, n = find_rows(uexp)
    return _parse(uexp, o + 4, n)
