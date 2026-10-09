"""ASR track logic (the .sif's LOGC chunk): placed objects ("locators") and triggers.

Layout (010-Editor-Templates sasr_zif.bt, LogicHeader / Locator):
    u32 name_hash, u32 version, u32 num_triggers, u32 num_locators,
    u32 triggers_offset, u32 attributes_offset, u32 locators_offset   (offsets from the chunk start)
    Locator (80 bytes): u32 group, locator, mesh_forest, mesh_tree, setup_object, animated_instance,
        sub_data hashes, u32 flags, u32 health, f32 sequence_start_frame_multiplier,
        f32 sequence_inter_spawn_multiplier, f32 animated_instance_playback_speed,
        f32[4] position (x, y, z, 1), f32[4] rotation quaternion (x, y, z, w)

On Rocky Coaster (Samba_Hard) the 31 locators place item.Forest trees: item boxes, orange balloons,
loudspeakers, bats and TVs. Tree hashes match forest_tree_dump's index (tree_<n>_hash_<signed hash>).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass
class Locator:
    group: int
    name: int
    forest: int
    tree: int
    setup: int
    animated_instance: int
    sub_data: int
    flags: int
    health: int
    start_frame_multiplier: float
    inter_spawn_multiplier: float
    playback_speed: float
    position: tuple
    rotation: tuple   # quaternion (x, y, z, w)


def parse_logic(data: bytes) -> list[Locator]:
    _, _, _, n_loc, _, _, loc_off = struct.unpack_from("<7I", data, 0)
    out = []
    for i in range(n_loc):
        v = struct.unpack_from("<9I3f4f4f", data, loc_off + 80 * i)
        out.append(Locator(*v[:12], tuple(v[12:15]), tuple(v[16:20])))
    return out
