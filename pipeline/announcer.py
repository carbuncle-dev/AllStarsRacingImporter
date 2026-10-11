"""Make the announcer stop calling the replaced course "E-Stadium".

The course call ("voice_announce_coursecall_stg1016") is a cue in each voice language's
VOICE_ANNOUNCE.acb (loose CRI files in pakchunk 4-9 / 15, mounted at UNION/Content[/L10N/<lang>]/CriWareData).
There is no "Rocky Coaster" recording, so the cue is pointed at the generic "Special Track" line
(voice_announce_coursecall_special_1): its Cue row takes that cue's ReferenceType/ReferenceIndex/Length/
NumRelatedWaveforms, patched in place (cw.utf.field_offset). The AWB is untouched, so the ACB's stream
hash stays valid. course_text sets the matching subtitle and ships these ACBs in the text pak.

Extract the originals first (repak, game AES key):
    repak --aes-key <key> unpack -o extracted/cw/voice -i <mount>/VOICE_ANNOUNCE.acb <pakchunkN-Windows.pak>
"""

from __future__ import annotations

import struct
from pathlib import Path

from cw.utf import field_offset, parse_utf
from pipeline.ue_build import ROOT

SOURCE = ROOT / "extracted" / "cw" / "voice" / "UNION" / "Content"
OUT = ROOT / "out" / "text" / "voice"
REPLACEMENT = "voice_announce_coursecall_special_1"
FIELDS = ("ReferenceType", "ReferenceIndex", "Length", "NumRelatedWaveforms")


def patch_acb(data: bytes, stage: str) -> bytes:
    acb = bytearray(data)
    head = parse_utf(data)[0]
    names = parse_utf(head["CueNameTable"])
    index = {names[i]["CueName"]: names[i]["CueIndex"] for i in range(len(names.rows))}
    mine, theirs = index[f"voice_announce_coursecall_{stage}"], index[REPLACEMENT]
    cue_blob = head["CueTable"]
    base = acb.find(cue_blob)
    if base < 0 or acb.find(cue_blob, base + 1) >= 0:
        raise ValueError("CueTable not found once in the ACB")
    cues = parse_utf(cue_blob)
    for f in FIELDS:
        if cues[mine][f] == cues[theirs][f]:
            continue
        try:
            off, fmt = field_offset(cue_blob, mine, f)
        except KeyError:  # column with one shared (default) value: nothing per cue to change
            raise ValueError(f"{f} differs but is not stored per cue")
        struct.pack_into(fmt, acb, base + off, cues[theirs][f])
    return bytes(acb)


def build(stage: str) -> list[tuple[Path, str]]:
    """Patched ACBs -> [(local file, mount path relative to UNION/Content)]."""
    out = []
    for src in sorted(SOURCE.rglob("VOICE_ANNOUNCE.acb")):
        rel = src.relative_to(SOURCE).as_posix()
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(patch_acb(src.read_bytes(), stage))
        out.append((dst, rel))
    return out
