"""Write the flow height and seated placements in the export shape Unreal already reads."""

from __future__ import annotations

import json
import struct
import subprocess
from contextlib import closing
from pathlib import Path

from regionflow.store import Region, connect

from reflow.heightio import write_gray16
from reflow.manifest import CELLS, SAMPLES, quantize

_MESH_KINDS = {"mesh", "cell"}


def _flow_grid(region: Region, chunk_x: int, chunk_y: int) -> list[list[float]]:
    """Export sample order: index 0 is the high corner."""
    chunk = region.chunks[(chunk_x, chunk_y)]
    blob = region.tile("height", chunk_x, chunk_y)
    count = chunk.w * chunk.h
    codes = struct.unpack("<" + "H" * count, blob)
    scale = region.info["height_scale_m"]
    offset = region.info["height_offset_m"]

    def meters(col: int, row: int) -> float:
        return codes[row * chunk.w + col] * scale + offset

    return [
        [meters(CELLS - ix, CELLS - iy) for ix in range(SAMPLES)]
        for iy in range(SAMPLES)
    ]


def _png_rows(grid: list[list[float]]) -> tuple[float, float, list[list[int]]]:
    values = [value for row in grid for value in row]
    low = min(values)
    high = max(values)
    scale = 1.0 if high <= low else (high - low) / 65535.0
    rows = [[quantize(value, low, scale) for value in row] for row in grid]
    return low, high, rows


def write_terrain(region: Region, source_terrain: Path, dest: Path) -> int:
    folder = dest / "terrain" / "1"
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for (chunk_x, chunk_y), _chunk in sorted(region.chunks.items()):
        block_x, block_y = -chunk_x, -chunk_y
        meta_path = source_terrain / f"x{block_x}_y{block_y}.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        grid = _flow_grid(region, chunk_x, chunk_y)
        low, high, rows = _png_rows(grid)
        meta["height_min_m"] = low
        meta["height_max_m"] = high
        (folder / meta_path.name).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        write_gray16(folder / f"x{block_x}_y{block_y}_height.png", rows)
        written += 1
    return written


def write_placements(region: Region, dest: Path) -> int:
    folder = dest / "placements" / "1"
    folder.mkdir(parents=True, exist_ok=True)
    by_block: dict[tuple[int, int], list[tuple[int, dict]]] = {}
    count = 0
    for feature in region.features("seated_assets"):
        props = feature["properties"]
        block_x, block_y = props["block"]
        placed = dict(props["placement"])
        placed["position_m"] = list(feature["geometry"]["coordinates"])
        by_block.setdefault((block_x, block_y), []).append((int(props["index"]), placed))
        count += 1
    for (block_x, block_y), rows in by_block.items():
        objects = [obj for _index, obj in sorted(rows)]
        document = {"block": [block_x, block_y], "objects": objects}
        (folder / f"x{block_x}_y{block_y}.json").write_text(
            json.dumps(document, indent=2), encoding="utf-8"
        )
    return count


def write_manifest(source_manifest: Path, dest: Path, blocks: list[list[int]]) -> None:
    document = json.loads(source_manifest.read_text(encoding="utf-8"))
    files = []
    for row in document.get("files") or []:
        kind = row.get("kind")
        path = str(row.get("path") or "").replace("\\", "/")
        if kind == "texture" or (kind in _MESH_KINDS and path.endswith(".gltf")):
            copied = dict(row)
            copied["path"] = "../bree/" + path
            files.append(copied)
    out = {
        "format_version": document.get("format_version"),
        "generator": "lotro_reflow",
        "region": {"id": 1, "blocks": blocks},
        "files": files,
        "errors": [],
        "warnings": [],
    }
    (dest / "manifest.json").write_text(json.dumps(out), encoding="utf-8")


def _windows_path(path: Path) -> str | None:
    text = path.resolve().as_posix()
    if len(text) < 7 or not text.startswith("/mnt/") or text[6] != "/":
        return None
    return text[5].upper() + ":\\" + text[7:].replace("/", "\\")


def link_source_dirs(export_dir: Path, dest: Path) -> None:
    """The material reader opens ``<export>/meshes/<id>.gltf`` beside the placements.

    The manifest already points mesh import at ``../bree``. These links serve that
    second lookup without copying the mesh library. Unreal runs on Windows, so a
    drvfs tree gets a junction rather than a Linux symlink.
    """
    for name in ("meshes", "textures"):
        link = dest / name
        target = export_dir / name
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            continue
        link_win = _windows_path(link)
        target_win = _windows_path(target)
        if link_win and target_win:
            subprocess.run(
                ["cmd.exe", "/c", "mklink", "/J", link_win, target_win],
                check=True,
                cwd="/mnt/c",
            )
            continue
        link.symlink_to(Path("..") / export_dir.name / name, target_is_directory=True)


def publish(db_path: Path, export_dir: Path, dest: Path) -> dict[str, int]:
    export_dir = Path(export_dir)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    link_source_dirs(export_dir, dest)
    with closing(connect(db_path, readonly=True)) as db:
        region = Region(db)
        tiles = write_terrain(region, export_dir / "terrain" / "1", dest)
        placed = write_placements(region, dest)
        blocks = sorted([-cx, -cy] for (cx, cy) in region.chunks)
    write_manifest(export_dir / "manifest.json", dest, blocks)
    return {"terrain_tiles": tiles, "placed_objects": placed}
