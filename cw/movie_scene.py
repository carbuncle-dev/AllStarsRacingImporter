"""Curve channels of cooked UE 5.4 Level Sequences (MovieScene double/float channels), patched in place.

TMovieSceneCurveChannelImpl::Serialize (Engine/Source/Runtime/MovieScene/.../MovieSceneCurveChannelImpl.cpp):
    u8 PreInfinityExtrap, u8 PostInfinityExtrap,
    i32 4, i32 n, n x i32 key frame (tick resolution units),
    i32 value size (32 double / 28 float), i32 n, n x value (Value first, then tangents and modes),
    Value DefaultValue, u32 bHasDefaultValue, i32 tick numerator, i32 tick denominator, u32 bShowCurve.
Channels are found by that signature. A 3D transform section's channels come in order: location X Y Z,
rotation X (roll) Y (pitch) Z (yaw), scale X Y Z.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class Channel:
    offset: int          # of the time element size (i32 4)
    times: list[int]
    values: list[float]
    value_offsets: list[int]
    default: float
    default_offset: int
    double: bool


def find_channels(data: bytes) -> list[Channel]:
    out, o = [], 0
    while o < len(data) - 16:
        es, n = struct.unpack_from("<ii", data, o)
        if es == 4 and 0 < n < 5000:
            t_end = o + 8 + 4 * n
            if t_end + 8 <= len(data):
                vs, vn = struct.unpack_from("<ii", data, t_end)
                if vs in (32, 28) and vn == n:
                    times = list(struct.unpack_from(f"<{n}i", data, o + 8))
                    if all(times[i] <= times[i + 1] for i in range(n - 1)):
                        fmt = "<d" if vs == 32 else "<f"
                        offs = [t_end + 8 + k * vs for k in range(n)]
                        vals = [struct.unpack_from(fmt, data, p)[0] for p in offs]
                        end = t_end + 8 + n * vs
                        out.append(Channel(o, times, vals, offs, struct.unpack_from(fmt, data, end)[0], end, vs == 32))
                        o = end
                        continue
        o += 1
    return out


def transform_sections(channels: list[Channel]) -> list[list[Channel]]:
    """Runs of 9 consecutive double channels (location, rotation, scale)."""
    out, run = [], []
    for c in channels:
        if c.double and (not run or c.offset - run[-1].default_offset < 200):
            run.append(c)
        else:
            if run:
                out += [run[i:i + 9] for i in range(0, len(run) - 8, 9)]
            run = [c] if c.double else []
    if run:
        out += [run[i:i + 9] for i in range(0, len(run) - 8, 9)]
    return out


def value_at(c: Channel, t: int) -> float:
    """Value at time t: the key's value if keyed there, else the nearest earlier key (or the first)."""
    if not c.times:
        return c.default
    best = 0
    for i, ti in enumerate(c.times):
        if ti <= t:
            best = i
    return c.values[best]


def set_key(data: bytearray, c: Channel, t: int, value: float) -> bool:
    if t not in c.times:
        return False
    struct.pack_into("<d" if c.double else "<f", data, c.value_offsets[c.times.index(t)], value)
    return True
