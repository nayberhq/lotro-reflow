"""SQLite storage and the shared sampling contract. All coordinates are region-grid coordinates."""
from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from struct import Struct, unpack_from

FORMATS = {name: Struct("<" + fmt) for name, fmt in
           (("u8", "B"), ("u16", "H"), ("f32", "f"))}
APP_ID = 0x4C524631

DDL = (
    "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE chunks(x INTEGER, y INTEGER, w INTEGER, h INTEGER, PRIMARY KEY(x,y))",
    """CREATE TABLE products(name TEXT PRIMARY KEY, kind TEXT NOT NULL,
       producer TEXT NOT NULL, signature TEXT NOT NULL)""",
    """CREATE TABLE tiles(name TEXT REFERENCES products ON DELETE CASCADE,
       x INTEGER, y INTEGER, data BLOB NOT NULL, PRIMARY KEY(name,x,y),
       FOREIGN KEY(x,y) REFERENCES chunks)""",
    """CREATE TABLE features(name TEXT REFERENCES products ON DELETE CASCADE, id TEXT,
       xmin REAL, ymin REAL, xmax REAL, ymax REAL, body TEXT NOT NULL, PRIMARY KEY(name,id))""",
    "CREATE INDEX feature_bounds ON features(name,xmin,xmax)",
)


def connect(path, readonly=False):
    uri = Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc")
    db = sqlite3.connect(uri, uri=True, timeout=60, isolation_level=None)
    db.execute("PRAGMA foreign_keys=ON")
    return db


def check_schema(db, create=False):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    app = db.execute("PRAGMA application_id").fetchone()[0]
    if create and (app, version) == (0, 0) and not db.execute(
        "SELECT 1 FROM sqlite_master LIMIT 1"
    ).fetchone():
        for statement in DDL:
            db.execute(statement)
        db.execute(f"PRAGMA application_id={APP_ID}")
        db.execute("PRAGMA user_version=1")
    elif (app, version) != (APP_ID, 1):
        raise ValueError("Expected a Region Flow v1 database")


def meta(db):
    return {k: json.loads(v) for k, v in db.execute("SELECT key,value FROM meta")}


@dataclass(frozen=True)
class Chunk:
    x: int
    y: int
    w: int  # number of vertices, including the far edge
    h: int


class Region:
    """Read API supplied to flow steps. Make a fresh view after changing products."""

    def __init__(self, db):
        self._db = db
        self.info = meta(db)
        self.size = self.info["chunk_cells"]
        self.chunks = {(x, y): Chunk(x, y, w, h) for x, y, w, h in
                       db.execute("SELECT x,y,w,h FROM chunks ORDER BY y,x")}
        self.products = dict(db.execute("SELECT name,kind FROM products"))
        self.tile = lru_cache(maxsize=32)(self._tile)

    def _tile(self, name, x, y):
        if self.products.get(name) not in FORMATS:
            raise ValueError(f"Unknown grid {name}")
        row = self._db.execute("SELECT data FROM tiles WHERE name=? AND x=? AND y=?",
                               (name, x, y)).fetchone()
        if row is None:
            raise ValueError(f"Missing {name} tile at {(x, y)}")
        return row[0]

    def sample(self, name, gx, gy):
        """Read any integer vertex, including across tile seams. None means outside coverage."""
        fmt = FORMATS[self.products[name]]
        x, i = divmod(gx, self.size)
        y, j = divmod(gy, self.size)
        for cy in ((y, y - 1) if j == 0 else (y,)):
            for cx in ((x, x - 1) if i == 0 else (x,)):
                c = self.chunks.get((cx, cy))
                if c:
                    col, row = gx - cx * self.size, gy - cy * self.size
                    if col < c.w and row < c.h:
                        return fmt.unpack_from(self.tile(name, cx, cy), (row*c.w + col)*fmt.size)[0]
        return None

    def window(self, name, chunk, halo=0):
        """Read rows around a chunk. Missing vertices at a hole or outer edge are None."""
        if type(halo) is not int or halo < 0:
            raise ValueError("halo must be a nonnegative number of vertices")
        fmt = FORMATS[self.products[name]]
        row_format = "<" + str(chunk.w) + fmt.format[-1]
        buf = self.tile(name, chunk.x, chunk.y)
        x0, y0 = chunk.x * self.size, chunk.y * self.size
        rows = []
        for local_y in range(-halo, chunk.h + halo):
            gy = y0 + local_y
            if local_y < 0 or local_y >= chunk.h:
                row = [self.sample(name, x0+i, gy) for i in range(-halo, chunk.w+halo)]
            else:
                row = [self.sample(name, x0+i, gy) for i in range(-halo, 0)]
                row.extend(unpack_from(row_format, buf, local_y * chunk.w * fmt.size))
                row.extend(self.sample(name, x0+i, gy) for i in range(chunk.w, chunk.w+halo))
            rows.append(row)
        return rows

    def bounds_m(self, chunk):
        x, y = chunk.x * self.size, chunk.y * self.size
        return (*self.world_xy(x, y), *self.world_xy(x + chunk.w - 1, y + chunk.h - 1))

    def world_xy(self, gx, gy):
        return tuple(o + g*s for o, g, s in zip(self.info["origin_m"], (gx, gy),
                                                  self.info["spacing_m"]))

    def features(self, name, bounds=None):
        """Complete region-global features; optional inclusive world-meter bounding box."""
        if self.products.get(name) != "features":
            raise ValueError(f"Unknown feature collection {name}")
        sql = "SELECT body FROM features WHERE name=?"
        args = [name]
        if bounds is not None:
            xmin, ymin, xmax, ymax = bounds
            sql += " AND xmin<=? AND xmax>=? AND ymin<=? AND ymax>=?"
            args += [xmax, xmin, ymax, ymin]
        for (body,) in self._db.execute(sql + " ORDER BY id", args):
            yield json.loads(body)


def put_feature(db, name, feature):
    fid = feature.get("id")
    if feature.get("type") != "Feature" or not isinstance(fid, str) or not fid:
        raise ValueError("Each feature needs a stable nonempty string id")
    geom = feature["geometry"]
    coords = geom["coordinates"]
    kind = geom["type"]
    if kind == "Point":
        points = [coords]
    elif kind == "LineString" and len(coords) >= 2:
        points = coords
    elif kind == "Polygon" and coords and all(len(r) >= 4 and r[0] == r[-1] for r in coords):
        points = [p for ring in coords for p in ring]
    else:
        raise ValueError(f"Unsupported or invalid geometry for {fid}: {kind}")
    if any(len(p) not in (2, 3) or not all(type(v) in (float, int) and math.isfinite(v)
                                           for v in p) for p in points):
        raise ValueError(f"Invalid coordinates for {fid}")
    xs, ys = zip(*[(p[0], p[1]) for p in points])
    db.execute("INSERT INTO features VALUES(?,?,?,?,?,?,?)",
               (name, fid, min(xs), min(ys), max(xs), max(ys),
                json.dumps(feature, allow_nan=False, sort_keys=True)))


def verify_glue(region, name):
    """Compare exact encoded edge samples and diagonally touching corners."""
    stride = FORMATS[region.products[name]].size

    def edge(c, dx, dy):
        buf = region.tile(name, c.x, c.y)
        if dx and dy:
            col, row = c.w-1 if dx > 0 else 0, c.h-1 if dy > 0 else 0
            pos = (row*c.w+col)*stride
            return buf[pos:pos+stride]
        if dy:
            start = (c.h-1)*c.w*stride if dy > 0 else 0
            return buf[start:start+c.w*stride]
        start = (c.w-1)*stride if dx > 0 else 0
        return b"".join(buf[p:p+stride] for p in range(start, len(buf), c.w*stride))

    for (x, y), c in region.chunks.items():
        for dx, dy in ((1, 0), (0, 1), (1, 1), (-1, 1)):
            n = region.chunks.get((x+dx, y+dy))
            if n is None:
                continue
            if dx > 0 and c.w <= region.size or dx < 0 and n.w <= region.size:
                continue
            if dy and c.h <= region.size:
                continue
            if edge(c, dx, dy) != edge(n, -dx, -dy):
                raise ValueError(f"Seam mismatch in {name} between {(x,y)} and {(n.x,n.y)}")
