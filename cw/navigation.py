"""CrossWorlds navigation graph (UUnionNavigationBinaryDataAsset.Parameter.Data).

Reverse-engineered layout (little-endian, 64-bit offsets from blob start):
  Header (0x30):
    u32 magic 0x15EAE41C, u32 version (16), u32 0,
    u32 num_waypoints, u32 num_markers, u32 unknown_count,
    u64 waypoints_offset (0x30), u64 markers_offset, f32 total_length, u32 0
  Waypoints (0xF0 each), similar in spirit to Sumo's NavWaypoint:
    +00 float4 pos, +10 float4 dir, +20 float4 up            (Y up)
    +30 u64 to_links      -> num_to entries in the link-pointer table
    +38 u64 from_links    -> num_from inline Link records
    +40 u64 0, +48 u64 group -> a waypoint (section head)
    +50 u32 0, +54 u32 ?, +58 u32 num_to, +5C u32 num_from
    +60..+EF  widths, flags (+74), left/right edges (+80/+A0), centre (+C0), misc
  Links (0x50 each), stored per waypoint in waypoint order:
    +00 float4 from->to direction, +30 u64 from waypoint, +38 u64 to waypoint,
    +40 f32 length (== distance between the two waypoints)
  Link-pointer table: per waypoint, u64 link pointers padded to 16 bytes, in waypoint order
  Markers (0x50 each): float4 pos, dir, up, u32 type, then size/params

Pointer fields are resolved to indices; every other byte is kept verbatim so
unknown fields survive a parse/write round trip.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

MAGIC = 0x15EAE41C
HEADER = 0x30
WP = 0xF0
LINK = 0x50
PTR = 8
MARKER = 0x50


@dataclass
class Link:
    raw: bytearray      # 0x50 bytes, pointer fields zeroed
    from_wp: int
    to_wp: int

    @property
    def length(self) -> float:
        return struct.unpack_from("<f", self.raw, 0x40)[0]


@dataclass
class Waypoint:
    raw: bytearray      # 0xF0 bytes, pointer fields zeroed
    to_links: list[int]     # link indices
    from_links: list[int]   # link indices (inline records)
    group: int              # waypoint index, -1 if none

    def vec(self, off: int) -> np.ndarray:
        return np.array(struct.unpack_from("<3f", self.raw, off))

    pos = property(lambda self: self.vec(0x00))
    dir = property(lambda self: self.vec(0x10))
    up = property(lambda self: self.vec(0x20))
    left = property(lambda self: self.vec(0x80))
    right = property(lambda self: self.vec(0xA0))
    flags = property(lambda self: struct.unpack_from("<I", self.raw, 0x74)[0])


@dataclass
class Marker:
    raw: bytes

    pos = property(lambda self: np.array(struct.unpack_from("<3f", self.raw, 0x00)))
    type = property(lambda self: struct.unpack_from("<I", self.raw, 0x30)[0])


@dataclass
class Navigation:
    version: int
    unknown_count: int
    total_length: float
    waypoints: list[Waypoint]
    links: list[Link]
    markers: list[Marker]


def parse_navigation(d: bytes) -> Navigation:
    magic, version, _, nw, nm, nx = struct.unpack_from("<6I", d, 0)
    if magic != MAGIC:
        raise ValueError(f"bad navigation magic {magic:#x}")
    wp_off, mk_off = struct.unpack_from("<QQ", d, 0x18)
    total = struct.unpack_from("<f", d, 0x28)[0]
    links_off = wp_off + nw * WP

    def wp_index(p):
        return -1 if p == 0 else (p - wp_off) // WP

    link_index: dict[int, int] = {}
    links: list[Link] = []
    waypoints: list[Waypoint] = []
    to_ptrs = []
    for i in range(nw):
        o = wp_off + i * WP
        raw = bytearray(d[o:o + WP])
        to_arr, from_arr, _, group = struct.unpack_from("<4Q", raw, 0x30)
        n_to, n_from = struct.unpack_from("<2I", raw, 0x58)
        struct.pack_into("<4Q", raw, 0x30, 0, 0, 0, 0)
        from_ids = []
        for k in range(n_from):
            lo = from_arr + k * LINK
            lraw = bytearray(d[lo:lo + LINK])
            a, b = struct.unpack_from("<QQ", lraw, 0x30)
            struct.pack_into("<QQ", lraw, 0x30, 0, 0)
            link_index[lo] = len(links)
            from_ids.append(len(links))
            links.append(Link(lraw, wp_index(a), wp_index(b)))
        to_ptrs.append([struct.unpack_from("<Q", d, to_arr + k * PTR)[0] for k in range(n_to)])
        waypoints.append(Waypoint(raw, [], from_ids, wp_index(group)))
    for w, ptrs in zip(waypoints, to_ptrs):
        w.to_links = [link_index[p] for p in ptrs]
    assert links_off + len(links) * LINK <= mk_off
    markers = [Marker(d[mk_off + i * MARKER:mk_off + (i + 1) * MARKER]) for i in range(nm)]
    return Navigation(version, nx, total, waypoints, links, markers)


def write_navigation(nav: Navigation) -> bytes:
    nw, nl = len(nav.waypoints), len(nav.links)
    wp_off = HEADER
    links_off = wp_off + nw * WP
    table_off = links_off + nl * LINK
    table_size = sum(-(-len(w.to_links) * PTR // 16) * 16 for w in nav.waypoints)
    mk_off = table_off + table_size

    wp_ptr = lambda i: 0 if i < 0 else wp_off + i * WP
    out = bytearray(mk_off + len(nav.markers) * MARKER)
    struct.pack_into("<6I", out, 0, MAGIC, nav.version, 0, nw, len(nav.markers), nav.unknown_count)
    struct.pack_into("<QQfI", out, 0x18, wp_off, mk_off, nav.total_length, 0)

    # Links are laid out in waypoint order, each waypoint's from-links contiguous.
    order = [li for w in nav.waypoints for li in w.from_links]
    assert sorted(order) == list(range(nl)), "every link must belong to exactly one waypoint"
    link_ptr = {li: links_off + k * LINK for k, li in enumerate(order)}
    for li, p in link_ptr.items():
        link = nav.links[li]
        out[p:p + LINK] = link.raw
        struct.pack_into("<QQ", out, p + 0x30, wp_ptr(link.from_wp), wp_ptr(link.to_wp))

    entry = table_off
    for i, w in enumerate(nav.waypoints):
        o = wp_off + i * WP
        out[o:o + WP] = w.raw
        to_arr = entry if w.to_links else 0
        for k, li in enumerate(w.to_links):
            struct.pack_into("<Q", out, entry + k * PTR, link_ptr[li])
        entry += -(-len(w.to_links) * PTR // 16) * 16
        from_arr = link_ptr[w.from_links[0]] if w.from_links else 0
        struct.pack_into("<4Q", out, o + 0x30, to_arr, from_arr, 0, wp_ptr(w.group))
        struct.pack_into("<2I", out, o + 0x58, len(w.to_links), len(w.from_links))

    for k, m in enumerate(nav.markers):
        out[mk_off + k * MARKER:mk_off + (k + 1) * MARKER] = m.raw
    return bytes(out)
