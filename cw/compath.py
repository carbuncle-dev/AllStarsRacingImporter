"""CrossWorlds CPU/AI paths (UUnionComPathBinaryDataAsset.RawData).

Reverse-engineered layout (little-endian, packed, no alignment):
    u32 magic 0x0003D10C, u32 num_points
    num_points x Point: float3 pos, float3 dir, float3 up              (Y up)
    PathList "paths":    u32 count, count x Path
    PathList "segments": u32 count, count x Path   (paths split into sections, e.g. MAIN_0..MAIN_7)
    routing section -- parsed as raw bytes + two "order" arrays + tail for byte-exact round
    trips, but actually laid out as (decoded from stg1034/stg1901; see encode_routing):
        u32 num_junctions, Junction[num_junctions]
        u32 num_dash, Dash[num_dash]                                  (not decoded)
        i32 path_point_index[sum of all path line counts]             (identity: path points come first)
        per segment: i32 in_index[in_count], i32 out_index[out_count], i32 start_junction, i32 end_junction
        per junction: i32 in_segments[2], i32 out_segments[2], i32 branch_out_segment   (2-way junctions)
    Junction: String name, f32 pos[3], f32 unknown (~0.9), f32 radius (25),
              u32 kind (1 merge, 2 branch, 3 goal), u32 n, then count-prefixed arrays:
              u32 choices[n], f32 probabilities[n], u32 zero[n], u32 zero[n]
    Each 2-way junction appends 4 points after the path points: the split point on the
    non-branch path's in/out lines, then on the branch path's in/out lines. Segments start/end
    at those points; -1 means the path's own start/end.
Path:
    String name, String in_line, String out_line     (u32 length, chars, NUL; length excludes NUL)
    u32 in_count, u32 out_count, f32 in_length, f32 out_length,
    u32 path_type (1 MAIN, 2 sub/branch, 5 shortcut, 7 goal ...), u32 unknown, u32 zero[3],
    f32 in_distance[in_count], f32 out_distance[out_count]   (cumulative distance along the line)
When in_line == out_line the path is a single line; its points are stored once.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

import numpy as np

MAGIC = 0x0003D10C
POINT_DTYPE = np.dtype([("pos", "<f4", 3), ("dir", "<f4", 3), ("up", "<f4", 3)])


@dataclass
class Path:
    name: str
    in_line: str
    out_line: str
    in_length: float
    out_length: float
    path_type: int
    unknown: int
    in_distance: np.ndarray
    out_distance: np.ndarray
    reserved: tuple[int, int, int] = (0, 0, 0)


@dataclass
class ComPath:
    points: np.ndarray                 # structured POINT_DTYPE
    paths: list[Path]
    segments: list[Path]
    junctions_raw: bytes               # undecoded junction/dash section
    point_order_a: np.ndarray
    point_order_b: np.ndarray
    tail: tuple[int, int] = (-1, -1)
    extra: dict = field(default_factory=dict)
    routing_raw: bytes | None = None   # when set, written verbatim instead of junctions/orders/tail


class _Reader:
    def __init__(self, d: bytes, o: int = 0):
        self.d, self.o = d, o

    def u32(self) -> int:
        v = struct.unpack_from("<I", self.d, self.o)[0]
        self.o += 4
        return v

    def string(self) -> str:
        n = self.u32()
        s = self.d[self.o:self.o + n].decode("ascii")
        self.o += n + 1
        return s

    def floats(self, n: int) -> np.ndarray:
        a = np.frombuffer(self.d, "<f4", n, self.o).copy()
        self.o += 4 * n
        return a

    def path(self) -> Path:
        name, a, b = self.string(), self.string(), self.string()
        ni, no, li, lo, ptype, unk = struct.unpack_from("<2I2f2I", self.d, self.o)
        reserved = struct.unpack_from("<3I", self.d, self.o + 24)
        self.o += 36
        return Path(name, a, b, li, lo, ptype, unk, self.floats(ni), self.floats(no), reserved)

    def path_list(self) -> list[Path]:
        return [self.path() for _ in range(self.u32())]


def parse_compath(d: bytes) -> ComPath:
    magic, n = struct.unpack_from("<2I", d, 0)
    if magic != MAGIC:
        raise ValueError(f"bad ComPath magic {magic:#x}")
    points = np.frombuffer(d, POINT_DTYPE, n, 8).copy()
    r = _Reader(d, 8 + n * POINT_DTYPE.itemsize)
    paths, segments = r.path_list(), r.path_list()
    orders_at = len(d) - 2 * n * 4 - 8
    if orders_at < r.o:
        raise ValueError("point order arrays overlap path data")
    a = np.frombuffer(d, "<i4", n, orders_at).copy()
    b = np.frombuffer(d, "<i4", n, orders_at + 4 * n).copy()
    tail = struct.unpack_from("<2i", d, len(d) - 8)
    return ComPath(points, paths, segments, d[r.o:orders_at], a, b, tail)


def _string(s: str) -> bytes:
    raw = s.encode("ascii")
    return struct.pack("<I", len(raw)) + raw + b"\0"


def _path(p: Path) -> bytes:
    return (_string(p.name) + _string(p.in_line) + _string(p.out_line)
            + struct.pack("<2I2f2I3I", len(p.in_distance), len(p.out_distance), p.in_length, p.out_length,
                          p.path_type, p.unknown, *p.reserved)
            + np.asarray(p.in_distance, "<f4").tobytes() + np.asarray(p.out_distance, "<f4").tobytes())


def write_compath(c: ComPath) -> bytes:
    n = len(c.points)
    out = [struct.pack("<2I", MAGIC, n), np.ascontiguousarray(c.points, POINT_DTYPE).tobytes()]
    for lst in (c.paths, c.segments):
        out.append(struct.pack("<I", len(lst)))
        out.extend(_path(p) for p in lst)
    if c.routing_raw is not None:
        out.append(c.routing_raw)
    else:
        out.append(c.junctions_raw)
        out.append(np.asarray(c.point_order_a, "<i4").tobytes())
        out.append(np.asarray(c.point_order_b, "<i4").tobytes())
        out.append(struct.pack("<2i", *c.tail))
    return b"".join(out)


@dataclass
class Junction:
    name: str
    pos: tuple[float, float, float]
    unknown: float
    radius: float
    kind: int                          # 1 merge, 2 branch, 3 goal
    choices: list[int]
    probabilities: list[float]


def encode_routing(junctions: list[Junction], n_path_points: int,
                   segments: list[tuple[list[int], list[int], int, int]],
                   junction_segments: list[tuple[int, int, int, int, int]]) -> bytes:
    """Routing section for files without dash records (layout in the module docstring).

    segments: per segment (in point indices, out point indices, start junction, end junction)
    junction_segments: per junction (in_a, in_b, out_a, out_b, branch_out)
    """
    out = [struct.pack("<I", len(junctions))]
    for j in junctions:
        out.append(_string(j.name))
        out.append(struct.pack("<5f2I", *j.pos, j.unknown, j.radius, j.kind, len(j.choices)))
        n = len(j.choices)
        out.append(struct.pack(f"<I{n}I", n, *j.choices))
        out.append(struct.pack(f"<I{n}f", n, *j.probabilities))
        out.append(struct.pack(f"<I{n}I", n, *([0] * n)))
        out.append(struct.pack(f"<I{n}I", n, *([0] * n)))
    out.append(struct.pack("<I", 0))  # no dash records
    out.append(np.arange(n_path_points, dtype="<i4").tobytes())
    for in_idx, out_idx, start, end in segments:
        out.append(np.asarray(in_idx, "<i4").tobytes())
        out.append(np.asarray(out_idx, "<i4").tobytes())
        out.append(struct.pack("<2i", start, end))
    for js in junction_segments:
        out.append(struct.pack("<5i", *js))
    return b"".join(out)
