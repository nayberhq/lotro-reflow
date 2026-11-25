"""Adapters for region-wide rasters and tiled exports on a common vertex grid."""
import hashlib
import json
import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .store import FORMATS, Region, check_schema, meta, put_feature, verify_glue


def canonical(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      separators=(",", ":"), sort_keys=True).encode()


def signature(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_raster(path, fmt, width, height):
    if path.suffix.lower() == ".png":
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError("PNG intake needs Pillow (python -m pip install Pillow)") from exc
        with Image.open(path) as img:
            if img.size != (width, height):
                raise ValueError(f"{path}: expected {width} x {height}, got {img.size}")
            if fmt == "u16" and img.mode in ("I;16", "I;16L", "I;16B", "I"):
                data = img.convert("I;16").tobytes("raw", "I;16")
            elif fmt == "u8" and img.mode == "L":
                data = img.tobytes()
            else:
                raise ValueError(f"{path}: expected grayscale {fmt}, got {img.mode}")
    else:
        data = path.read_bytes()
    if len(data) != width * height * FORMATS[fmt].size:
        raise ValueError(f"{path}: incorrect byte count for {width} x {height} {fmt}")
    return data


def source_tiles(doc, base, size, formats):
    def read(files, w, h):
        if files.keys() != formats.keys():
            raise ValueError("Each raster must provide exactly the declared layers")
        return {name: read_raster(base / files[name], fmt, w, h)
                for name, fmt in formats.items()}

    if ("raster" in doc) == ("tiles" in doc):
        raise ValueError("Supply either a region raster or a list of tiles")
    if "tiles" in doc:
        for tile in doc["tiles"]:
            x, y = tile["chunk"]
            if type(x) is not int or type(y) is not int:
                raise ValueError("Chunk coordinates must be integers")
            yield x, y, size+1, size+1, read(tile["layers"], size+1, size+1)
        return

    raster = doc["raster"]
    width, height = raster["shape_xy"]
    if any(type(v) is not int or v < 2 for v in (width, height)):
        raise ValueError("A region raster needs at least two vertices on each axis")
    layers = read(raster["layers"], width, height)
    for row in range(0, height-1, size):
        for col in range(0, width-1, size):
            w, h = min(size+1, width-col), min(size+1, height-row)
            tile = {}
            for name, buf in layers.items():
                stride = FORMATS[formats[name]].size
                tile[name] = b"".join(buf[((row+j)*width+col)*stride:
                                          ((row+j)*width+col+w)*stride] for j in range(h))
            yield col//size, row//size, w, h, tile


def stage_source(stage, path):
    doc = load_json(path)
    if doc["schema"] != 1:
        raise ValueError("Unsupported source manifest version")
    info = {k: doc[k] for k in ("region", "spacing_m", "origin_m", "chunk_cells",
                                "height_scale_m", "height_offset_m")}
    if not isinstance(info["region"], str) or not info["region"].strip():
        raise ValueError("Missing stable region identifier")
    for key in ("origin_m", "spacing_m"):
        if len(info[key]) != 2 or not all(type(v) in (float, int) and math.isfinite(v)
                                          for v in info[key]):
            raise ValueError(f"Invalid {key}")
    if (min(info["spacing_m"]) <= 0 or type(info["chunk_cells"]) is not int
            or info["chunk_cells"] < 1):
        raise ValueError("Chunk size and spacing must be positive")
    if not all(type(info[k]) in (int, float) and math.isfinite(info[k])
               for k in ("height_scale_m", "height_offset_m")) or info["height_scale_m"] <= 0:
        raise ValueError("Invalid vertical transform")
    info["provenance"] = doc.get("provenance", {})
    stage.executemany("INSERT INTO meta VALUES(?,?)", [(k, json.dumps(v)) for k,v in info.items()])
    formats = doc["layers"]
    if formats.get("height") != "u16" or any(
        not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", k) or k == "features"
        or fmt not in ("u8", "u16") for k, fmt in formats.items()
    ):
        raise ValueError("Declare height as u16 and other source grids as u8 or u16")
    for name, kind in {**formats, "features": "features"}.items():
        stage.execute("INSERT INTO products VALUES(?,?,?,?)", (name, kind, "intake_map", ""))
    for x,y,w,h,layers in source_tiles(doc, path.parent, info["chunk_cells"], formats):
        stage.execute("INSERT INTO chunks VALUES(?,?,?,?)", (x,y,w,h))
        stage.executemany("INSERT INTO tiles VALUES(?,?,?,?)",
                          [(k,x,y,v) for k,v in layers.items()])
    if "features" in doc:
        collection = load_json(path.parent / doc["features"])
        if collection["type"] != "FeatureCollection":
            raise ValueError("Expected a FeatureCollection")
        for f in collection["features"]:
            put_feature(stage, "features", f)
    view = Region(stage)
    if not view.chunks:
        raise ValueError("Region is empty")
    for name in formats:
        verify_glue(view, name)
    # Include grid layout even when reading only a vector product. Downstream steps
    # may partition work differently when coverage or physical transforms change.
    layout = signature([info, list(stage.execute("SELECT * FROM chunks ORDER BY x,y"))])
    for name, kind in view.products.items():
        digest = hashlib.sha256(canonical([1, layout, name, kind]))
        if kind == "features":
            for f in view.features(name):
                digest.update(bytes.fromhex(signature(f)))
        else:
            for (data,) in stage.execute("SELECT data FROM tiles WHERE name=? ORDER BY x,y", (name,)):
                digest.update(hashlib.sha256(data).digest())
        stage.execute("UPDATE products SET signature=? WHERE name=?", (digest.hexdigest(),name))


def intake(db, path):
    """Stage and verify before publication. The caller owns a single write transaction."""
    with closing(sqlite3.connect("")) as stage:
        check_schema(stage, create=True)
        stage_source(stage, Path(path))
        old = meta(db)
        new = meta(stage)
        if old and old["region"] != new["region"]:
            raise ValueError(f"Database belongs to {old['region']}, received {new['region']}")
        select = "SELECT name,signature FROM products WHERE producer='intake_map'"
        if dict(db.execute(select)) == dict(stage.execute(select)):
            return "reused"
        # A changed source snapshot replaces all derived data, including obsolete chunks.
        for table in ("products", "chunks", "meta"):
            db.execute(f"DELETE FROM {table}")
        for table, columns in (("meta",2), ("chunks",4), ("products",4), ("tiles",4), ("features",7)):
            marks = ",".join(["?"] * columns)
            db.executemany(f"INSERT INTO {table} VALUES({marks})", stage.execute(f"SELECT * FROM {table}"))
        return "built"
