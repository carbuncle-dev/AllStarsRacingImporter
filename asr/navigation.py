"""ASR navigation data (TRAK chunk, version 8), layout from CppSLib/SlMod NavData.

All pointers are offsets from the start of the chunk. Vectors are stored as
float3 padded to 16 bytes. Only the fields needed for porting are decoded.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

Vec3 = tuple[float, float, float]

WAYPOINT_SIZE = 0xB0
LINK_SIZE = 0xA0
RACING_LINE_SIZE = 0x20
SEGMENT_SIZE = 0x40
MARKER_SIZE = 0x60


@dataclass
class Link:
    offset: int
    from_wp: int = -1
    to_wp: int = -1
    left: Vec3 = (0, 0, 0)
    right: Vec3 = (0, 0, 0)
    racing_line_limit_left: Vec3 = (0, 0, 0)
    racing_line_limit_right: Vec3 = (0, 0, 0)
    length: float = 0.0
    width: float = 0.0
    cross_section: list[Vec3] = field(default_factory=list)


@dataclass
class Waypoint:
    index: int
    name: str
    pos: Vec3
    dir: Vec3
    up: Vec3
    track_dist: float
    flags: int
    target_speed: float
    to_links: list[int]    # link offsets
    from_links: list[int]


@dataclass
class RacingLineSegment:
    racing_line: Vec3
    safe_racing_line: Vec3
    link: int              # link offset
    length: float
    turn_type: int


@dataclass
class RacingLine:
    looping: bool
    total_length: float
    segments: list[RacingLineSegment]


@dataclass
class TrackMarker:
    index: int
    pos: Vec3
    dir: Vec3
    up: Vec3
    type: int
    radius: float
    waypoint: int          # waypoint index, -1 if none
    value: int
    track_dist: float
    linked_marker: int     # marker index, -1 if none
    jump_speed_percentage: float
    flags: int
    text: str


@dataclass
class Navigation:
    version: int
    waypoints: list[Waypoint]
    links: dict[int, Link]
    racing_lines: list[RacingLine]
    track_markers: list[TrackMarker]
    total_track_dist: float


class _Reader:
    def __init__(self, data: bytes):
        self.d = data

    def u32(self, o): return struct.unpack_from("<I", self.d, o)[0]
    def i32(self, o): return struct.unpack_from("<i", self.d, o)[0]
    def f32(self, o): return struct.unpack_from("<f", self.d, o)[0]
    def vec3(self, o): return struct.unpack_from("<3f", self.d, o)

    def string(self, o, size):
        raw = self.d[o:o + size]
        return raw[:raw.find(b"\0")].decode("ascii", "replace") if b"\0" in raw else raw.decode("ascii", "replace")


def parse_navigation(data: bytes) -> Navigation:
    r = _Reader(data)
    version = r.i32(0x4)
    if version >= 9:
        raise NotImplementedError(f"navigation version {version} (ASRT layout) not supported")
    num_waypoints, num_racing_lines, num_markers = r.i32(0x18), r.i32(0x1C), r.i32(0x20)
    # Pointer block after the 14 header fields (hash, version, remapped, 11 counts):
    # verts, normals, tris, waypoints, racing lines, markers, traffic routes,
    # traffic spawn, blockers, errors, spatial groups.
    ptrs = struct.unpack_from("<11I", data, 0x38)
    wp_ptr, rl_ptr, tm_ptr = ptrs[3], ptrs[4], ptrs[5]
    tail = 0x38 + 11 * 4
    total_track_dist = r.f32(tail)

    wp_index = {wp_ptr + i * WAYPOINT_SIZE: i for i in range(num_waypoints)}
    tm_index = {tm_ptr + i * MARKER_SIZE: i for i in range(num_markers)}
    links: dict[int, Link] = {}

    def read_link(o: int) -> Link:
        link = links.get(o)
        if link is None:
            n_cross, cross_ptr = r.i32(o + 0x88), r.u32(o + 0x8C)
            link = Link(
                offset=o,
                right=r.vec3(o + 0x10), left=r.vec3(o + 0x20),
                racing_line_limit_left=r.vec3(o + 0x30), racing_line_limit_right=r.vec3(o + 0x40),
                length=r.f32(o + 0x80), width=r.f32(o + 0x84),
                cross_section=[r.vec3(cross_ptr + k * 16) for k in range(n_cross)] if cross_ptr else [],
            )
            links[o] = link
        return link

    waypoints = []
    for i in range(num_waypoints):
        o = wp_ptr + i * WAYPOINT_SIZE
        n_to, n_from = r.i32(o + 0x78), r.i32(o + 0x7C)
        to_arr, from_arr = r.u32(o + 0x80), r.u32(o + 0x84)
        # ToLinks: pointer to an array of link pointers; FromLinks: pointer to inline links.
        to_links = [r.u32(to_arr + k * 4) for k in range(n_to)] if to_arr else []
        from_links = [from_arr + k * LINK_SIZE for k in range(n_from)] if from_arr else []
        for lo in to_links:
            read_link(lo).from_wp = i
        for lo in from_links:
            read_link(lo).to_wp = i
        waypoints.append(Waypoint(
            index=i, name=r.string(o + 0x30, 0x40),
            pos=r.vec3(o), dir=r.vec3(o + 0x10), up=r.vec3(o + 0x20),
            track_dist=r.f32(o + 0x70), flags=r.i32(o + 0x74), target_speed=r.f32(o + 0x88),
            to_links=to_links, from_links=from_links,
        ))

    racing_lines = []
    for i in range(num_racing_lines):
        o = rl_ptr + i * RACING_LINE_SIZE
        n_seg, seg_ptr = r.i32(o), r.u32(o + 4)
        segments = []
        for k in range(n_seg):
            s = seg_ptr + k * SEGMENT_SIZE
            segments.append(RacingLineSegment(
                racing_line=r.vec3(s), safe_racing_line=r.vec3(s + 0x10),
                link=r.u32(s + 0x28), length=r.f32(s + 0x2C), turn_type=r.i32(s + 0x30),
            ))
        racing_lines.append(RacingLine(looping=r.i32(o + 8) != 0, total_length=r.f32(o + 0x10), segments=segments))

    markers = []
    for i in range(num_markers):
        o = tm_ptr + i * MARKER_SIZE
        markers.append(TrackMarker(
            index=i, pos=r.vec3(o), dir=r.vec3(o + 0x10), up=r.vec3(o + 0x20),
            type=r.i32(o + 0x30), radius=r.f32(o + 0x34),
            waypoint=wp_index.get(r.u32(o + 0x38), -1), value=r.i32(o + 0x3C),
            track_dist=r.f32(o + 0x40), linked_marker=tm_index.get(r.u32(o + 0x44), -1),
            jump_speed_percentage=r.f32(o + 0x48), flags=r.i32(o + 0x4C), text=r.string(o + 0x50, 0x10),
        ))

    return Navigation(version, waypoints, links, racing_lines, markers, total_track_dist)
