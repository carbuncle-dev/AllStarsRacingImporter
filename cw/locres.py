"""Unreal .locres (localised text tables, version 3 "Optimized_CityHash64_UTF16"): read, edit, write.

Layout: 16-byte magic, u8 version, i64 offset of the string array, u32 entry count, u32 namespace
count; per namespace: u32 hash + FString name, u32 key count; per key: u32 hash + FString key, u32
source-string hash, i32 index into the string array. String array: i32 count, then FString + i32
reference count per string. FString: i32 length (negative = UTF-16 code units), text + terminator.
Everything read is kept (hashes, string encodings), so an unedited table writes back byte for byte.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = bytes.fromhex("0E147475674A03FC4A15909DC3377F1B")


def _read_fstring(d: bytes, o: int) -> tuple[str, int, bool]:
    """(text, next offset, stored as UTF-16)"""
    n = struct.unpack_from("<i", d, o)[0]
    o += 4
    if n == 0:
        return "", o, False
    if n < 0:
        end = o - 2 * n
        return d[o:end - 2].decode("utf-16-le"), end, True
    return d[o:o + n - 1].decode("latin-1"), o + n, False


def _write_fstring(s: str, wide: bool = False) -> bytes:
    if not s:
        return struct.pack("<i", 0)
    if not wide:
        try:
            raw = s.encode("latin-1") + bytes(1)
            return struct.pack("<i", len(raw)) + raw
        except UnicodeEncodeError:
            pass
    raw = s.encode("utf-16-le") + bytes(2)
    return struct.pack("<i", -(len(raw) // 2)) + raw


@dataclass
class Entry:
    key_hash: int
    key: str
    key_wide: bool
    source_hash: int
    index: int


@dataclass
class Namespace:
    hash: int
    name: str
    wide: bool
    entries: list[Entry] = field(default_factory=list)


@dataclass
class Locres:
    version: int
    namespaces: list[Namespace]
    strings: list[str]
    refcounts: list[int]
    wide: list[bool]

    def find(self, namespace: str, key: str) -> Entry:
        for ns in self.namespaces:
            if ns.name == namespace:
                for e in ns.entries:
                    if e.key == key:
                        return e
        raise KeyError((namespace, key))

    def text(self, namespace: str, key: str) -> str:
        return self.strings[self.find(namespace, key).index]

    def set_text(self, namespace: str, key: str, text: str) -> None:
        """Change one key's text; if other keys share its string they keep the old one."""
        e = self.find(namespace, key)
        if self.refcounts[e.index] > 1:
            self.refcounts[e.index] -= 1
            self.strings.append(text)
            self.refcounts.append(1)
            self.wide.append(self.wide[e.index])
            e.index = len(self.strings) - 1
        else:
            self.strings[e.index] = text


def parse_locres(d: bytes) -> Locres:
    if d[:16] != MAGIC or d[16] != 3:
        raise ValueError("unsupported locres (need version 3)")
    table = struct.unpack_from("<q", d, 17)[0]
    o = 25 + 4  # skip the entry count (recomputed on write)
    ns_count = struct.unpack_from("<I", d, o)[0]
    o += 4
    namespaces = []
    for _ in range(ns_count):
        h = struct.unpack_from("<I", d, o)[0]
        name, o, w = _read_fstring(d, o + 4)
        ns = Namespace(h, name, w)
        kc = struct.unpack_from("<I", d, o)[0]
        o += 4
        for _ in range(kc):
            kh = struct.unpack_from("<I", d, o)[0]
            key, o, kw = _read_fstring(d, o + 4)
            sh, idx = struct.unpack_from("<Ii", d, o)
            o += 8
            ns.entries.append(Entry(kh, key, kw, sh, idx))
        namespaces.append(ns)
    if o != table:
        raise ValueError("key table does not end at the string array")
    count = struct.unpack_from("<i", d, o)[0]
    o += 4
    strings, refs, wide = [], [], []
    for _ in range(count):
        s, o, w = _read_fstring(d, o)
        strings.append(s)
        wide.append(w)
        refs.append(struct.unpack_from("<i", d, o)[0])
        o += 4
    if o != len(d):
        raise ValueError("trailing data after the string array")
    return Locres(3, namespaces, strings, refs, wide)


def write_locres(loc: Locres) -> bytes:
    body = bytearray()
    entries = 0
    body += struct.pack("<I", len(loc.namespaces))
    for ns in loc.namespaces:
        body += struct.pack("<I", ns.hash) + _write_fstring(ns.name, ns.wide) + struct.pack("<I", len(ns.entries))
        for e in ns.entries:
            body += struct.pack("<I", e.key_hash) + _write_fstring(e.key, e.key_wide)
            body += struct.pack("<Ii", e.source_hash, e.index)
            entries += 1
    head = MAGIC + bytes([loc.version])
    table = len(head) + 8 + 4 + len(body)
    out = bytearray(head + struct.pack("<qI", table, entries) + body)
    out += struct.pack("<i", len(loc.strings))
    for s, r, w in zip(loc.strings, loc.refcounts, loc.wide):
        out += _write_fstring(s, w) + struct.pack("<i", r)
    return bytes(out)
