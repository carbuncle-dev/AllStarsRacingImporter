"""Reader for Sonic & SEGA All-Stars Racing (2010 PC) .xpac archives.

Layout (little-endian):
    header  : u32 0, u32 0, u32 alignment, u32 count, u32 0, u32 0
    entries : count x (u32 name_hash, u32 offset, u32 size, u32 size, u32 0)
Each entry is a zlib stream. Names are resolved via a hash->path table
(MAPPING.GC from TomKannenberg/xpac-Tool). .zif/.zig entries decompress to
.sif/.sig payloads prefixed with a u32 length.
"""

from __future__ import annotations

import argparse
import fnmatch
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MAPPING = Path(__file__).resolve().parent.parent / "third_party" / "xpac-Tool" / "MAPPING.GC"


@dataclass
class XpacEntry:
    name_hash: int
    offset: int
    size: int
    name: str | None


def load_mapping(path: Path = DEFAULT_MAPPING) -> dict[int, str]:
    mapping: dict[int, str] = {}
    for line in path.read_text(encoding="latin-1").splitlines():
        line = line.strip().rstrip(";")
        if ":" not in line:
            continue
        h, name = line.split(":", 1)
        name = name.replace("\\", "/").lstrip("./")
        mapping[int(h)] = name
    return mapping


class Xpac:
    def __init__(self, path: Path, mapping: dict[int, str] | None = None):
        self.path = Path(path)
        mapping = mapping if mapping is not None else load_mapping()
        with open(self.path, "rb") as f:
            _, _, self.alignment, count, _, _ = struct.unpack("<6I", f.read(24))
            table = f.read(count * 20)
        self.entries = []
        for i in range(count):
            h, off, size, _, _ = struct.unpack_from("<5I", table, i * 20)
            self.entries.append(XpacEntry(h, off, size, mapping.get(h)))

    def read_raw(self, entry: XpacEntry) -> bytes:
        with open(self.path, "rb") as f:
            f.seek(entry.offset)
            return f.read(entry.size)

    def read(self, entry: XpacEntry) -> bytes:
        """Decompressed payload; .zif/.zig length prefixes are stripped."""
        raw = self.read_raw(entry)
        data = zlib.decompress(raw) if raw[:1] == b"\x78" else raw
        if len(data) >= 4:
            prefix = struct.unpack_from("<I", data)[0]
            if prefix in (len(data) - 4, len(data)):
                data = data[4:]
        return data

    def find(self, pattern: str) -> list[XpacEntry]:
        pattern = pattern.lower()
        return [e for e in self.entries if e.name and fnmatch.fnmatch(e.name.lower(), pattern)]


def output_name(entry: XpacEntry) -> str:
    """Archive path with .zif/.zig renamed to their decompressed .sif/.sig form."""
    if entry.name is None:
        return f"unknown/{entry.name_hash:08x}.bin"
    p = Path(entry.name)
    ext = {".zif": ".sif", ".zig": ".sig"}.get(p.suffix.lower(), p.suffix)
    return str(p.with_suffix(ext))


def main() -> None:
    ap = argparse.ArgumentParser(description="List or extract entries from an ASR .xpac archive")
    ap.add_argument("xpac", type=Path)
    ap.add_argument("-o", "--out", type=Path, help="extract matching entries to this folder")
    ap.add_argument("-p", "--pattern", default="*", help="glob over archive paths, e.g. '*samba_easy*'")
    args = ap.parse_args()

    pak = Xpac(args.xpac)
    entries = pak.entries if args.pattern == "*" else pak.find(args.pattern)
    for e in entries:
        print(f"{e.name_hash:08x} {e.size:>10} {e.name or '?'}")
        if args.out:
            dest = args.out / output_name(e)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(pak.read(e))
    unnamed = sum(e.name is None for e in pak.entries)
    print(f"{len(entries)} listed, {unnamed}/{len(pak.entries)} entries without a known name")


if __name__ == "__main__":
    main()
