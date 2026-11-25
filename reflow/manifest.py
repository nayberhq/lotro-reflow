"""Turn a Bree landblock export into a Region Flow manifest.

Export spacing is negative: sample 0 is the high world-X and world-Y corner, and a
higher block index sits at a lower coordinate. Region Flow stores positive spacing
with column 0 on the low-X side. Chunk coordinates are the negated block indices so
the shared high-X edge of one chunk is the low-X edge of the next.
"""

from __future__ import annotations

import json
import math
import struct
from collections import defaultdict
from pathlib import Path

from reflow.heightio import read_gray16

BLOCK_M = 160.0
SPACING_M = 5.0
SAMPLES = 33
CELLS = 32
WINDOW = (112, 124, 116, 127)
# Grid vertex (0, 0) sits one block southwest of landblock (0, 0)'s high corner,
# which is where the flipped sample 32 of that block lands.
ORIGIN_M = (-BLOCK_M, -BLOCK_M)

Block = tuple[int, int]
Grid = list[list[float]]


def chunk_of(block_x: int, block_y: int) -> tuple[int, int]:
    return (-block_x, -block_y)


def source_world(block_x: int, block_y: int, ix: int, iy: int) -> tuple[float, float]:
    """Engine XY of an export sample. Index 0 is the high corner."""
    return (
        -block_x * BLOCK_M - ix * SPACING_M,
        -block_y * BLOCK_M - iy * SPACING_M,
    )


def _vertex_key(x: float, y: float) -> tuple[int, int]:
    return (round(x * 1000.0), round(y * 1000.0))


def average_shared(grids: dict[Block, Grid]) -> dict[Block, Grid]:
    """Copy the grids and give every copy of a vertex the same elevation."""
    copied = {key: [row[:] for row in grid] for key, grid in grids.items()}
    groups: dict[tuple[int, int], list[tuple[Block, int, int]]] = defaultdict(list)
    for block, grid in copied.items():
        for iy, row in enumerate(grid):
            for ix in range(len(row)):
                groups[_vertex_key(*source_world(block[0], block[1], ix, iy))].append((block, iy, ix))
    for members in groups.values():
        if len(members) < 2:
            continue
        mean = sum(copied[block][iy][ix] for block, iy, ix in members) / len(members)
        for block, iy, ix in members:
            copied[block][iy][ix] = mean
    return copied


def height_transform(grids: dict[Block, Grid]) -> tuple[float, float]:
    """One u16 scale for the whole window. Flat ground uses a dummy positive scale."""
    values = [value for grid in grids.values() for row in grid for value in row]
    low = min(values)
    high = max(values)
    if high <= low:
        return low, 1.0
    return low, (high - low) / 65535.0


def quantize(meters: float, offset: float, scale: float) -> int:
    code = int(round((meters - offset) / scale))
    return max(0, min(65535, code))


def pack_tile(grid: Grid, offset: float, scale: float) -> bytes:
    """Little-endian u16, column 0 on the low-X side and row 0 on the low-Y side."""
    out = bytearray(SAMPLES * SAMPLES * 2)
    for row in range(SAMPLES):
        for col in range(SAMPLES):
            src_x = SAMPLES - 1 - col
            src_y = SAMPLES - 1 - row
            code = quantize(grid[src_y][src_x], offset, scale)
            struct.pack_into("<H", out, (row * SAMPLES + col) * 2, code)
    return bytes(out)


def encode_blocks(grids: dict[Block, Grid]) -> tuple[float, float, dict[tuple[int, int], bytes]]:
    """Average seams, quantize once, and return r16 bytes keyed by chunk."""
    for block, grid in grids.items():
        if len(grid) != SAMPLES or any(len(row) != SAMPLES for row in grid):
            raise ValueError(f"block {block} is not {SAMPLES} by {SAMPLES}")
    averaged = average_shared(grids)
    offset, scale = height_transform(averaged)
    tiles = {
        chunk_of(*block): pack_tile(grid, offset, scale) for block, grid in averaged.items()
    }
    return offset, scale, tiles


def _locate(world: float) -> tuple[int, float]:
    """Landblock index and sample position along one axis. Sample 0 is the high side."""
    quot = -world / BLOCK_M
    if abs(quot - round(quot)) < 1e-6:
        block = int(round(quot))
    else:
        block = math.floor(quot)
    index = (world - (-block * BLOCK_M)) / -SPACING_M
    return block, index


def _bilinear(grid: Grid, ix: float, iy: float) -> float | None:
    if ix < 0.0 or iy < 0.0 or ix > CELLS or iy > CELLS:
        return None
    x0 = min(CELLS, max(0, math.floor(ix)))
    y0 = min(CELLS, max(0, math.floor(iy)))
    tx = 0.0 if x0 == CELLS else ix - x0
    ty = 0.0 if y0 == CELLS else iy - y0
    x1 = x0 if tx == 0.0 else x0 + 1
    y1 = y0 if ty == 0.0 else y0 + 1
    return (
        (1.0 - tx) * (1.0 - ty) * grid[y0][x0]
        + tx * (1.0 - ty) * grid[y0][x1]
        + (1.0 - tx) * ty * grid[y1][x0]
        + tx * ty * grid[y1][x1]
    )


def sample_ground(grids: dict[Block, Grid], x: float, y: float) -> float | None:
    """Bilinear elevation from export-order grids, before the shared quantize.

    A vertex on a block boundary is stored twice. The low side of a block is the
    high side of the next block, so a window edge still resolves inside the window.
    """
    block_x, ix = _locate(x)
    block_y, iy = _locate(y)
    candidates = [(block_x, ix, block_y, iy)]
    if abs(ix) < 1e-6:
        candidates.append((block_x - 1, float(CELLS), block_y, iy))
    if abs(iy) < 1e-6:
        candidates.append((block_x, ix, block_y - 1, float(CELLS)))
    if abs(ix) < 1e-6 and abs(iy) < 1e-6:
        candidates.append((block_x - 1, float(CELLS), block_y - 1, float(CELLS)))
    for bx, sx, by, sy in candidates:
        grid = grids.get((bx, by))
        if grid is not None:
            return _bilinear(grid, sx, sy)
    return None


def _meters(codes: list[list[int]], low: float, high: float) -> Grid:
    span = high - low
    return [
        [low if span == 0.0 else low + (code / 65535.0) * span for code in row]
        for row in codes
    ]


def _load_block(folder: Path, block_x: int, block_y: int) -> tuple[dict, Grid]:
    path = folder / f"x{block_x}_y{block_y}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    spacing = float(meta["spacing_m"])
    samples = meta["samples"]
    if spacing != -SPACING_M or samples != [SAMPLES, SAMPLES]:
        raise ValueError(f"{path.name} is not a {SAMPLES}-sample tile at {-SPACING_M} m")
    codes = read_gray16(folder / f"x{block_x}_y{block_y}_height.png")
    if len(codes) != SAMPLES or any(len(row) != SAMPLES for row in codes):
        raise ValueError(f"height PNG for {block_x},{block_y} is not {SAMPLES} square")
    return meta, _meters(codes, float(meta["height_min_m"]), float(meta["height_max_m"]))


def _placement_features(folder: Path, grounds: dict[Block, Grid], window: tuple[int, int, int, int]) -> list[dict]:
    x0, y0, x1, y1 = window
    features = []
    for block_y in range(y0, y1 + 1):
        for block_x in range(x0, x1 + 1):
            path = folder / f"x{block_x}_y{block_y}.json"
            document = json.loads(path.read_text(encoding="utf-8"))
            for index, obj in enumerate(document.get("objects") or []):
                mesh = obj.get("mesh_id") or ""
                position = obj.get("position_m")
                if not mesh or not isinstance(position, list) or len(position) != 3:
                    continue
                x, y, z = (float(value) for value in position)
                ground = sample_ground(grounds, x, y)
                features.append(
                    {
                        "type": "Feature",
                        "id": f"{block_x}_{block_y}_{index}",
                        "properties": {
                            "block": [block_x, block_y],
                            "index": index,
                            "source_ground_m": ground,
                            "placement": obj,
                        },
                        "geometry": {"type": "Point", "coordinates": [x, y, z]},
                    }
                )
    return features


def write_manifest(
    export_dir: Path,
    dest: Path,
    window: tuple[int, int, int, int] = WINDOW,
) -> Path:
    """Write ``map.json``, height tiles, and placement features. Returns the map path."""
    export_dir = Path(export_dir)
    dest = Path(dest)
    terrain = export_dir / "terrain" / "1"
    x0, y0, x1, y1 = window
    original: dict[Block, Grid] = {}
    for block_y in range(y0, y1 + 1):
        for block_x in range(x0, x1 + 1):
            _meta, grid = _load_block(terrain, block_x, block_y)
            original[(block_x, block_y)] = grid
    offset, scale, tiles = encode_blocks(original)
    tile_dir = dest / "tiles"
    tile_dir.mkdir(parents=True, exist_ok=True)
    listed = []
    for (chunk_x, chunk_y), payload in sorted(tiles.items()):
        name = f"c{chunk_x}_{chunk_y}.r16"
        (tile_dir / name).write_bytes(payload)
        listed.append({"chunk": [chunk_x, chunk_y], "layers": {"height": f"tiles/{name}"}})
    features = _placement_features(export_dir / "placements" / "1", original, window)
    (dest / "placements.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=2),
        encoding="utf-8",
    )
    document = {
        "schema": 1,
        "region": "eriador/bree-ground",
        "origin_m": [ORIGIN_M[0], ORIGIN_M[1]],
        "spacing_m": [SPACING_M, SPACING_M],
        "height_scale_m": scale,
        "height_offset_m": offset,
        "chunk_cells": CELLS,
        "layers": {"height": "u16"},
        "tiles": listed,
        "features": "placements.json",
        "provenance": {
            "source": str(export_dir),
            "window": [x0, y0, x1, y1],
            "axis": "export sample 0 is high X/Y; flow column 0 is low X/Y; chunk is negated block",
        },
    }
    map_path = dest / "map.json"
    map_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return map_path
