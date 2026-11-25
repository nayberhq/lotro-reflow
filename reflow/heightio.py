"""16-bit grayscale PNG read and write for landblock height tiles."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    dist_left = abs(estimate - left)
    dist_up = abs(estimate - up)
    dist_up_left = abs(estimate - up_left)
    if dist_left <= dist_up and dist_left <= dist_up_left:
        return left
    if dist_up <= dist_up_left:
        return up
    return up_left


def read_gray16(path: Path) -> list[list[int]]:
    """Rows of big-endian 16-bit samples, top row first."""
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"not a PNG file: {path}")
    offset = 8
    width = height = bit_depth = color_type = -1
    idat = bytearray()
    while offset + 8 <= len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        offset += 4
        tag = data[offset : offset + 4]
        offset += 4
        chunk = data[offset : offset + length]
        offset += length + 4
        if tag == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", chunk[:10])
        elif tag == b"IDAT":
            idat.extend(chunk)
        elif tag == b"IEND":
            break
    if bit_depth != 16 or color_type != 0:
        raise ValueError(f"PNG must be 16-bit gray: {path}")
    raw = zlib.decompress(bytes(idat))
    stride = width * 2
    rows: list[list[int]] = []
    previous = bytearray(stride)
    cursor = 0
    for _y in range(height):
        filt = raw[cursor]
        cursor += 1
        row = bytearray(raw[cursor : cursor + stride])
        cursor += stride
        if filt == 1:
            for index in range(stride):
                left = row[index - 1] if index else 0
                row[index] = (row[index] + left) & 255
        elif filt == 2:
            for index in range(stride):
                row[index] = (row[index] + previous[index]) & 255
        elif filt == 3:
            for index in range(stride):
                left = row[index - 1] if index else 0
                row[index] = (row[index] + ((left + previous[index]) // 2)) & 255
        elif filt == 4:
            for index in range(stride):
                left = row[index - 1] if index else 0
                up = previous[index]
                up_left = previous[index - 1] if index else 0
                row[index] = (row[index] + _paeth(left, up, up_left)) & 255
        elif filt != 0:
            raise ValueError(f"unsupported PNG filter {filt} in {path}")
        previous = row
        rows.append([(row[index] << 8) | row[index + 1] for index in range(0, stride, 2)])
    return rows


def write_gray16(path: Path, rows: list[list[int]]) -> None:
    """Write filter-none 16-bit gray. ``rows`` are sample codes, top row first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    height = len(rows)
    width = len(rows[0]) if height else 0
    if height == 0 or any(len(row) != width for row in rows):
        raise ValueError("height PNG needs a rectangular grid")
    ihdr = struct.pack(">IIBBBBB", width, height, 16, 0, 0, 0, 0)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for value in row:
            raw.extend(struct.pack(">H", value & 0xFFFF))
    compressed = zlib.compress(bytes(raw), level=9)
    png = bytearray(b"\x89PNG\r\n\x1a\n")
    for tag, payload in ((b"IHDR", ihdr), (b"IDAT", compressed), (b"IEND", b"")):
        png.extend(struct.pack(">I", len(payload)))
        png.extend(tag)
        png.extend(payload)
        png.extend(struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))
    path.write_bytes(png)
