"""CRI @UTF tables (ACB cue sheets, CPK tables): read and write.

Big-endian. Only reading and in-place patching of fixed-size values are supported. Header after '@UTF' + u32 size (offsets below are relative to byte 8):
    u16 version, u16 rows offset, u32 strings offset, u32 data offset, u32 table-name string offset,
    u16 column count, u16 row width, u32 row count
Columns: u8 flags (0x10 name, 0x20 default value, 0x40 per-row value; low nibble = type), u32 name
string offset, then the default value when 0x20 is set.
Types: 0/1 u8/s8, 2/3 u16/s16, 4/5 u32/s32, 6/7 u64/s64, 8 f32, 9 f64, 0xA string (u32 offset into the
string table), 0xB data (u32 offset, u32 size into the data area, which holds nested @UTF tables).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

FORMATS = {0: ">B", 1: ">b", 2: ">H", 3: ">h", 4: ">I", 5: ">i", 6: ">Q", 7: ">q", 8: ">f", 9: ">d", 0xA: ">I", 0xB: ">II"}


@dataclass
class Column:
    name: str
    flags: int
    default: object = None

    @property
    def type(self) -> int:
        return self.flags & 0x0F

    @property
    def per_row(self) -> bool:
        return bool(self.flags & 0x40)


@dataclass
class UtfTable:
    name: str
    columns: list[Column]
    rows: list[dict] = field(default_factory=list)
    version: int = 1
    encoding_tail: bytes = b""

    def __getitem__(self, i: int) -> dict:
        return self.rows[i]


def _value(data: bytes, pos: int, typ: int, strings: int, blob: int):
    fmt = FORMATS[typ]
    v = struct.unpack_from(fmt, data, pos)
    if typ == 0xA:
        end = data.index(b"\0", strings + v[0])
        return data[strings + v[0]:end].decode("utf-8"), struct.calcsize(fmt)
    if typ == 0xB:
        return data[blob + v[0]:blob + v[0] + v[1]], 8
    return v[0], struct.calcsize(fmt)


def parse_utf(buf: bytes) -> UtfTable:
    if buf[:4] != b"@UTF":
        raise ValueError("not a @UTF table")
    size = struct.unpack_from(">I", buf, 4)[0]
    data = buf[8:8 + size]
    version, rows_off, strings, blob, name_off, ncol, width, nrow = struct.unpack_from(">HHIIIHHI", data, 0)
    string = lambda o: data[strings + o:data.index(b"\0", strings + o)].decode("utf-8")
    pos, columns = 24, []
    for _ in range(ncol):
        flags, name = struct.unpack_from(">BI", data, pos)
        pos += 5
        col = Column(string(name), flags)
        if flags & 0x20:
            col.default, n = _value(data, pos, flags & 0x0F, strings, blob)
            pos += n
        columns.append(col)
    rows = []
    for r in range(nrow):
        pos = rows_off + r * width
        row = {}
        for col in columns:
            if col.per_row:
                row[col.name], n = _value(data, pos, col.type, strings, blob)
                pos += n
            else:
                row[col.name] = col.default
        rows.append(row)
    return UtfTable(string(name_off), columns, rows, version)


def field_offset(buf: bytes, row: int, column: str) -> tuple[int, str]:
    """Byte offset (in buf) and struct format of a per-row value, for patching fixed-size fields in place."""
    size = struct.unpack_from(">I", buf, 4)[0]
    data = buf[8:8 + size]
    _, rows_off, strings, _, _, ncol, width, nrow = struct.unpack_from(">HHIIIHHI", data, 0)
    if not 0 <= row < nrow:
        raise IndexError(row)
    pos, at = 24, rows_off + row * width
    for _ in range(ncol):
        flags, name = struct.unpack_from(">BI", data, pos)
        pos += 5
        fmt = FORMATS[flags & 0x0F]
        if flags & 0x20:
            pos += struct.calcsize(fmt)
        col = data[strings + name:data.index(b"\0", strings + name)].decode("utf-8")
        if flags & 0x40:
            if col == column:
                if flags & 0x0F in (0xA, 0xB):
                    raise ValueError(f"{column} is not a fixed-size value")
                return 8 + at, fmt
            at += struct.calcsize(fmt)
    raise KeyError(column)
