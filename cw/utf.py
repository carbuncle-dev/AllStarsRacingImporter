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


def build_utf(table: UtfTable, blob_align: int = 1) -> bytes:
    """Serialize a table (the layout CRI's tools write: header, columns, rows, string pool starting with the
    table name, data area). With blob_align > 1 the data area starts at a multiple of it (counted from the
    '@UTF' magic) and every blob is placed at a multiple of it -- top-level ACB/ACF tables use 32, which keeps
    an embedded memory AWB aligned."""
    strings = bytearray()
    string_at: dict[str, int] = {}

    def string(s: str) -> int:
        if s not in string_at:
            string_at[s] = len(strings)
            strings.extend(s.encode("utf-8") + b"\0")
        return string_at[s]

    string(table.name)
    for col in table.columns:
        string(col.name)
    blobs: list[bytes] = []

    def encode(typ: int, value) -> tuple[bytes, object]:
        if typ == 0xA:
            return b"", ("str", string(value or ""))
        if typ == 0xB:
            blobs.append(bytes(value or b""))
            return b"", ("blob", len(blobs) - 1)
        return struct.pack(FORMATS[typ], value), None

    # pass 1: strings and blob order (column defaults first, then rows), so offsets are known
    col_parts = []
    for col in table.columns:
        part = [struct.pack(">B", col.flags), ("str", string(col.name))]
        if col.flags & 0x20:
            raw, ref = encode(col.type, col.default)
            part.append(raw if ref is None else ref)
        col_parts.append(part)
    row_parts = []
    for row in table.rows:
        parts = []
        for col in table.columns:
            if col.per_row:
                raw, ref = encode(col.type, row[col.name])
                parts.append(raw if ref is None else ref)
        row_parts.append(parts)

    columns_size = sum(5 + (struct.calcsize(FORMATS[c.type]) if c.flags & 0x20 else 0) for c in table.columns)
    width = sum(struct.calcsize(FORMATS[c.type]) for c in table.columns if c.per_row)
    rows_off = 24 + columns_size
    strings_off = rows_off + width * len(table.rows)
    data_off = strings_off + len(strings)
    if blob_align > 1:
        data_off += (-(8 + data_off)) % blob_align
    blob_at, data = [], bytearray()
    for b in blobs:
        if blob_align > 1 and b:
            data.extend(b"\0" * ((-len(data)) % blob_align))
        blob_at.append((len(data) if b else 0, len(b)))
        data.extend(b)

    def resolve(p) -> bytes:
        if isinstance(p, bytes):
            return p
        kind, i = p
        return struct.pack(">I", i) if kind == "str" else struct.pack(">II", *blob_at[i])

    body = bytearray(struct.pack(">HHIIIHHI", table.version, rows_off, strings_off, data_off, 0,
                                 len(table.columns), width, len(table.rows)))
    for part in col_parts:
        body += b"".join(resolve(p) for p in part)
    assert len(body) == rows_off
    for parts in row_parts:
        body += b"".join(resolve(p) for p in parts)
    body += strings
    body += b"\0" * (data_off - len(body))
    body += data
    body += b"\0" * ((-len(body)) % 8)
    return b"@UTF" + struct.pack(">I", len(body)) + bytes(body)
