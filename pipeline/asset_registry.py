"""Register new packages in the game's cooked AssetRegistry.bin (UE 5.4, FAssetRegistryVersion 17).

    patched = register_copies(registry_bytes, {"/Game/.../stg1016/SL_stg1016": "/Game/.../asr3001/SL_asr3001", ...})

The game only streams level packages the registry knows. A relocated stage (pipeline.relocate_stage) ships
its levels under new package names, so each new package gets a copy of the registry entries of the stock
package it was made from (asset data: class, tags, bundles, chunks, flags; package data), under the new names.
Everything else is copied byte for byte; new names are appended to the name batch so existing indices hold.

Layout (FAssetRegistryState::Save, FAssetRegistryWriter):
  header      FGuid version guid, int32 version, uint32 bFilterEditorOnlyData
  name batch  uint32 Num, uint32 NumStringBytes, uint64 HashVersion, uint64 hashes[Num],
              uint16be headers[Num] (bit 15 = UTF-16, low 15 bits = length), strings
  tag store   FixedTagPrivate (BeginMagic .. EndMagic), opaque here
  body        int32 AssetCount, FAssetData[AssetCount] (sorted by object path),
              int64 DependencySectionSize + section (opaque),
              int32 PackageDataCount, {FName, FAssetPackageData}[] (sorted by package name)
FName in the body: uint32 index into the name batch (bit 31 set: a uint32 number follows).
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

VERSION = 17  # AssetPackageDataHasExtension
HASH_ALGORITHM = 0xC1640000  # FNameHash::AlgorithmId (CityHash64 of the lower-case name)
END_MAGIC = struct.pack("<I", 0x87654321)
NUMBERED = 0x80000000


def name_hash(name: str, wide: bool) -> int:
    from cityhash import CityHash64
    return CityHash64(name.lower().encode("utf-16-le" if wide else "latin-1"))


class Reader:
    def __init__(self, data: bytes, pos: int = 0):
        self.d, self.p = data, pos

    def take(self, fmt: str):
        v = struct.unpack_from("<" + fmt, self.d, self.p)
        self.p += struct.calcsize("<" + fmt)
        return v if len(v) > 1 else v[0]

    def skip(self, n: int) -> None:
        self.p += n

    def name(self) -> tuple[int, int]:
        index = self.take("I")
        if index & NUMBERED:
            return index & ~NUMBERED, self.take("I")
        return index, 0

    def fstring(self) -> None:
        n = self.take("i")
        self.skip(n if n >= 0 else -2 * n)


@dataclass
class Asset:
    start: int
    end: int
    package_path: tuple[int, int]
    package_name: tuple[int, int]
    asset_name: tuple[int, int]


@dataclass
class Registry:
    data: bytes
    names: list[str]
    wide: list[bool]
    hashes: list[int]
    store: tuple[int, int]
    assets: list[Asset]
    deps: tuple[int, int]  # int64 size + section
    packages: list[tuple[tuple[int, int], int, int]]  # (name, start, end) incl. the key

    def text(self, n: tuple[int, int]) -> str:
        s = self.names[n[0]]
        return s if not n[1] else f"{s}_{n[1] - 1}"


def _asset(r: Reader) -> Asset:
    start = r.p
    package_path = r.name()
    r.name(); r.name()  # AssetClassPath (package, asset)
    package_name = r.name()
    asset_name = r.name()
    r.skip(8)  # tag map handle
    for _ in range(r.take("i")):  # bundles
        r.name()
        for _ in range(r.take("i")):
            r.name(); r.name(); r.fstring()  # FTopLevelAssetPath + SubPathString
    r.skip(4 * r.take("i"))  # chunk ids
    r.skip(4)  # package flags
    return Asset(start, r.p, package_path, package_name, asset_name)


def _package_data(r: Reader) -> None:
    r.skip(8 + 16)  # DiskSize, PackageGuid
    if r.take("I"):  # FMD5Hash.bIsValid
        r.skip(16)
    r.skip(r.take("i") * (12 + 20))  # ChunkHashes: FIoChunkId -> FIoHash
    r.skip(8 + 4 + 4)  # FileVersionUE, FileVersionLicenseeUE, Flags
    r.skip(r.take("i") * 20)  # custom versions (FGuid, int32)
    for _ in range(r.take("i")):  # ImportedClasses
        r.name()
    r.fstring()  # Extension


def _body(data: bytes, pos: int, num_names: int) -> tuple[list[Asset], tuple[int, int], list]:
    r = Reader(data, pos)
    count = r.take("i")
    if not 0 < count < 10_000_000:
        raise ValueError("asset count")
    assets = []
    for _ in range(count):
        a = _asset(r)
        if a.package_name[0] >= num_names:
            raise ValueError("name index")
        assets.append(a)
    deps_start = r.p
    r.skip(r.take("q"))
    deps = (deps_start, r.p)
    packages = []
    for _ in range(r.take("i")):
        start = r.p
        key = r.name()
        _package_data(r)
        packages.append((key, start, r.p))
    if r.p != len(data):
        raise ValueError(f"trailing {len(data) - r.p} bytes")
    return assets, deps, packages


def parse(data: bytes) -> Registry:
    r = Reader(data, 16)
    version, _filter_editor_only = r.take("iI")
    assert version == VERSION, f"registry version {version}"
    num, string_bytes, hash_version = r.take("IIQ")
    assert hash_version == HASH_ALGORITHM
    hashes = list(struct.unpack_from(f"<{num}Q", data, r.p)); r.skip(8 * num)
    headers = struct.unpack_from(f">{num}H", data, r.p); r.skip(2 * num)
    names, wide = [], []
    o = r.p
    for h in headers:
        n, w = h & 0x7FFF, bool(h & 0x8000)
        size = n * 2 if w else n
        names.append(data[o:o + size].decode("utf-16-le" if w else "latin-1"))
        wide.append(w)
        o += size
    assert o == r.p + string_bytes
    store_start = o
    # the tag store is opaque here: it ends at the EndMagic after which the body parses to the end of the file
    find = store_start
    while True:
        find = data.index(END_MAGIC, find + 1)
        try:
            assets, deps, packages = _body(data, find + 4, num)
            break
        except (ValueError, struct.error):
            continue
    return Registry(data, names, wide, hashes, (store_start, find + 4), assets, deps, packages)


def _write_name(index: int, number: int) -> bytes:
    return struct.pack("<II", index | NUMBERED, number) if number else struct.pack("<I", index)


def _rename(blob: bytes, r: Reader, fields: list[tuple[int, int]], new: list[tuple[int, int]]) -> bytes:
    """blob with the leading FNames at `fields` (offsets within blob) replaced, in order."""
    out, last = b"", 0
    for (off, size), n in zip(fields, new):
        out += blob[last:off] + _write_name(*n)
        last = off + size
    return out + blob[last:]


def register_copies(data: bytes, copies: dict[str, str]) -> tuple[bytes, list[str]]:
    """Registry bytes with every asset / package-data entry of each stock package (key) duplicated under the
    new package name (value; same leaf rename for the asset names). Returns (bytes, packages not found)."""
    reg = parse(data)
    names, wide, hashes = list(reg.names), list(reg.wide), list(reg.hashes)
    index = {}
    for i, n in enumerate(names):
        index.setdefault(n, i)

    def intern(s: str) -> tuple[int, int]:
        if s not in index:
            index[s] = len(names)
            w = any(ord(c) > 0xFF for c in s)
            names.append(s); wide.append(w); hashes.append(name_hash(s, w))
        return index[s], 0

    leaf = {old.rsplit("/", 1)[1]: new.rsplit("/", 1)[1] for old, new in copies.items()}
    existing = {reg.text(a.package_name) for a in reg.assets}
    missing = [old for old in copies if old not in existing]
    clash = [new for new in copies.values() if new in existing]
    assert not clash, f"already registered: {clash[:5]}"

    def sort_key(path: str) -> str:
        return path.lower()

    new_assets: list[tuple[str, bytes]] = []
    for a in reg.assets:
        old = reg.text(a.package_name)
        if old not in copies:
            continue
        new_pkg = copies[old]
        asset_name = reg.text(a.asset_name)
        new_asset = leaf.get(asset_name, asset_name)
        blob = data[a.start:a.end]
        r = Reader(blob)
        offs = []
        for _ in range(5):
            s = r.p
            r.name()
            offs.append((s, r.p - s))
        fields = [offs[0], offs[3], offs[4]]
        blob = _rename(blob, r, fields, [intern(new_pkg.rsplit("/", 1)[0]), intern(new_pkg), intern(new_asset)])
        new_assets.append((sort_key(f"{new_pkg}.{new_asset}"), blob))
    new_packages: list[tuple[str, bytes]] = []
    for key, start, end in reg.packages:
        old = reg.text(key)
        if old not in copies:
            continue
        blob = data[start:end]
        size = 8 if key[1] else 4
        new_packages.append((sort_key(copies[old]), _write_name(*intern(copies[old])) + blob[size:]))

    # merge in sorted position (the saved order; loaders don't depend on it, but keep it)
    def merged(entries: list[tuple[str, int, int]], extra: list[tuple[str, bytes]]) -> bytes:
        extra = sorted(extra)
        out, j = [], 0
        for k, s, e in entries:
            while j < len(extra) and extra[j][0] < k:
                out.append(extra[j][1]); j += 1
            out.append(data[s:e])
        out += [b for _, b in extra[j:]]
        return b"".join(out)

    assets = merged([(sort_key(f"{reg.text(a.package_name)}.{reg.text(a.asset_name)}"), a.start, a.end)
                     for a in reg.assets], new_assets)
    packages = merged([(sort_key(reg.text(k)), s, e) for k, s, e in reg.packages], new_packages)

    strings = b"".join(n.encode("utf-16-le" if w else "latin-1") for n, w in zip(names, wide))
    headers = b"".join(struct.pack(">H", (0x8000 if w else 0) | len(n)) for n, w in zip(names, wide))
    out = data[:24]
    out += struct.pack("<IIQ", len(names), len(strings), HASH_ALGORITHM)
    out += struct.pack(f"<{len(hashes)}Q", *hashes) + headers + strings
    out += data[reg.store[0]:reg.store[1]]
    out += struct.pack("<i", len(reg.assets) + len(new_assets)) + assets
    out += data[reg.deps[0]:reg.deps[1]]
    out += struct.pack("<i", len(reg.packages) + len(new_packages)) + packages
    return out, missing


STOCK = Path(__file__).resolve().parents[1] / "extracted" / "cw" / "newtrack_research" / "registry" / "UNION" / "AssetRegistry.bin"


def write_pak(copies: dict[str, str], pak: Path) -> None:
    """The game's registry with `copies` registered, as a loose-file pak (UNION/AssetRegistry.bin)."""
    from pipeline.music import UNREALPAK
    data, missing = register_copies(STOCK.read_bytes(), copies)
    if missing:
        print(f"registry: {len(missing)} packages not in the stock registry (left unregistered), e.g. {missing[:3]}")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "AssetRegistry.bin"
        f.write_bytes(data)
        response = Path(tmp) / "files.txt"
        response.write_text(f'"{f}" "../../../UNION/AssetRegistry.bin"\n')
        subprocess.run([str(UNREALPAK), str(pak), f"-create={response}"], check=True, capture_output=True)
    print(f"registry: {len(copies) - len(missing)} new packages registered -> {pak}")
