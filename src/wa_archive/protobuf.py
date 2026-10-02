"""Minimal protobuf wire-format decoder (no schema).

WhatsApp iOS stores some message details (quoted message ids, receipt and
reaction info) as protobuf blobs. We only need to walk the wire format.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

VARINT, I64, LEN, I32 = 0, 1, 2, 5


class DecodeError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    number: int
    wire_type: int
    value: int | bytes


def _varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise DecodeError("truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise DecodeError("varint too long")


def parse(buf: bytes) -> list[Field]:
    """Decode one message level. Raises DecodeError if buf isn't valid wire format."""
    fields: list[Field] = []
    pos = 0
    while pos < len(buf):
        key, pos = _varint(buf, pos)
        number, wt = key >> 3, key & 7
        if number == 0:
            raise DecodeError("field number 0")
        if wt == VARINT:
            val, pos = _varint(buf, pos)
        elif wt == I64:
            val, pos = buf[pos:pos + 8], pos + 8
        elif wt == I32:
            val, pos = buf[pos:pos + 4], pos + 4
        elif wt == LEN:
            n, pos = _varint(buf, pos)
            val, pos = buf[pos:pos + n], pos + n
        else:
            raise DecodeError(f"unsupported wire type {wt}")
        if pos > len(buf):
            raise DecodeError("truncated field")
        fields.append(Field(number, wt, val))
    return fields


def try_parse(buf: bytes) -> list[Field] | None:
    try:
        return parse(buf) if buf else None
    except DecodeError:
        return None


def first(fields: list[Field], number: int) -> Field | None:
    return next((f for f in fields if f.number == number), None)


def _classify(value: bytes) -> str:
    """Describe a LEN payload's shape without revealing it."""
    try:
        s = value.decode("utf-8")
    except UnicodeDecodeError:
        return "bytes"
    if not s:
        return "empty"
    if "@" in s and (s.endswith(".net") or s.endswith(".us") or s.endswith("@lid")):
        return "jid"
    if len(s) <= 8 and all(ord(c) > 0x2000 for c in s):
        return "emoji"
    if s.isalnum() and s.upper() == s and 16 <= len(s) <= 40:
        return "id"
    return "text"


def shape_census(buf: bytes, counter: Counter, prefix: str = "", depth: int = 0) -> None:
    """Count field paths and value kinds (e.g. '5:id', '3.1:jid'). Never records values."""
    fields = try_parse(buf)
    if fields is None:
        counter[f"{prefix or 'root'}:unparsed"] += 1
        return
    for f in fields:
        path = f"{prefix}.{f.number}" if prefix else str(f.number)
        if f.wire_type == LEN:
            nested = try_parse(f.value) if depth < 4 else None
            kind = _classify(f.value)
            if nested and kind in ("bytes", "text"):
                counter[f"{path}:msg"] += 1
                shape_census(f.value, counter, path, depth + 1)
            else:
                counter[f"{path}:{kind}"] += 1
        else:
            counter[f"{path}:{['varint', 'i64', '', '', '', 'i32'][f.wire_type]}"] += 1
