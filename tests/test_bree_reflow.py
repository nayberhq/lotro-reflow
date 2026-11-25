"""Seam orientation and asset seating for the bree_ground window."""

from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from regionflow.flow import build
from regionflow.steps import STEPS
from regionflow.store import Region, connect

from reflow.heightio import read_gray16, write_gray16
from reflow.manifest import SAMPLES, encode_blocks, quantize, sample_ground, source_world
from reflow.seat import STEPS as SEAT_STEPS


def _blank(fill: float) -> list[list[float]]:
    return [[fill] * SAMPLES for _ in range(SAMPLES)]


def _column(blob: bytes, column: int) -> list[int]:
    codes = struct.unpack("<" + "H" * (SAMPLES * SAMPLES), blob)
    return [codes[row * SAMPLES + column] for row in range(SAMPLES)]


class HeightPngTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        rows = [[index * 33 + column for column in range(4)] for index in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "h.png"
            write_gray16(path, rows)
            self.assertEqual(read_gray16(path), rows)


class ManifestTests(unittest.TestCase):
    def test_window_edge_stays_inside_the_block_that_owns_it(self) -> None:
        grid = _blank(0.0)
        grid[0][SAMPLES - 1] = 3.0
        world_x, world_y = source_world(0, 0, SAMPLES - 1, 0)
        self.assertAlmostEqual(sample_ground({(0, 0): grid}, world_x, world_y), 3.0)

    def test_flipped_tiles_share_one_edge_code(self) -> None:
        west = _blank(2.0)
        east = _blank(1.0)
        for row in range(SAMPLES):
            west[row][0] = 6.0
            east[row][SAMPLES - 1] = 4.0
        # Block 1 is west of block 0 because the block index grows toward -X.
        offset, scale, tiles = encode_blocks({(0, 0): east, (1, 0): west})
        expected = quantize(5.0, offset, scale)
        self.assertEqual(_column(tiles[(0, 0)], 0), [expected] * SAMPLES)
        self.assertEqual(_column(tiles[(-1, 0)], SAMPLES - 1), [expected] * SAMPLES)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for (chunk_x, chunk_y), payload in tiles.items():
                (root / f"c{chunk_x}_{chunk_y}.r16").write_bytes(payload)
            document = {
                "schema": 1,
                "region": "seam-test",
                "origin_m": [-160.0, -160.0],
                "spacing_m": [5.0, 5.0],
                "height_scale_m": scale,
                "height_offset_m": offset,
                "chunk_cells": 32,
                "layers": {"height": "u16"},
                "tiles": [
                    {"chunk": [chunk_x, chunk_y], "layers": {"height": f"c{chunk_x}_{chunk_y}.r16"}}
                    for chunk_x, chunk_y in sorted(tiles)
                ],
            }
            map_path = root / "map.json"
            map_path.write_text(json.dumps(document), encoding="utf-8")
            recipe = root / "recipe.json"
            recipe.write_text(json.dumps({"schema": 1, "steps": ["intake_map"]}), encoding="utf-8")
            database = root / "seam.sqlite"
            report = build(str(database), str(recipe), str(map_path), STEPS)
            self.assertEqual(report["intake_map"], "built")
            with closing(connect(database, readonly=True)) as db:
                region = Region(db)
                self.assertEqual(region.world_xy(32, 32), (0.0, 0.0))
                self.assertEqual(region.sample("height", 0, 0), expected)


class SeatTests(unittest.TestCase):
    def _run(self, codes: list[int], features: list[dict]) -> list[dict]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "height.r16").write_bytes(struct.pack("<4H", *codes))
            (root / "placements.json").write_text(
                json.dumps({"type": "FeatureCollection", "features": features}),
                encoding="utf-8",
            )
            (root / "map.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "region": "seat-test",
                        "origin_m": [0.0, 0.0],
                        "spacing_m": [1.0, 1.0],
                        "height_scale_m": 1.0,
                        "height_offset_m": 0.0,
                        "chunk_cells": 1,
                        "layers": {"height": "u16"},
                        "tiles": [{"chunk": [0, 0], "layers": {"height": "height.r16"}}],
                        "features": "placements.json",
                    }
                ),
                encoding="utf-8",
            )
            (root / "recipe.json").write_text(
                json.dumps({"schema": 1, "steps": ["intake_map", "seat_assets"]}),
                encoding="utf-8",
            )
            database = root / "seat.sqlite"
            registry = dict(STEPS)
            registry.update(SEAT_STEPS)
            build(str(database), str(root / "recipe.json"), str(root / "map.json"), registry)
            with closing(connect(database, readonly=True)) as db:
                region = Region(db)
                return list(region.features("seated_assets"))

    def test_uniform_lift_keeps_xy_and_distance(self) -> None:
        features = [
            _point("a", 0, 0, 9, 7),
            _point("b", 1, 0, 12, 7),
        ]
        seated = {item["id"]: item for item in self._run([10, 10, 10, 10], features)}
        first = seated["a"]["geometry"]["coordinates"]
        second = seated["b"]["geometry"]["coordinates"]
        self.assertEqual(first[:2], [0, 0])
        self.assertEqual(second[:2], [1, 0])
        self.assertAlmostEqual(first[2], 12)
        self.assertAlmostEqual(second[2], 15)
        before = math.dist((0, 0, 9), (1, 0, 12))
        after = math.dist(tuple(first), tuple(second))
        self.assertAlmostEqual(before, after)

    def test_local_ground_change_keeps_clearance(self) -> None:
        features = [
            _point("low", 0, 0, 2, 0),
            _point("high", 1, 0, 3, 1),
            _point("outside", 5, 5, 1, 0),
        ]
        seated = {item["id"]: item for item in self._run([0, 5, 0, 0], features)}
        self.assertEqual(seated["low"]["geometry"]["coordinates"], [0, 0, 2])
        self.assertEqual(seated["high"]["geometry"]["coordinates"], [1, 0, 7])
        self.assertEqual(seated["outside"]["geometry"]["coordinates"], [5, 5, 1])
        self.assertFalse(seated["outside"]["properties"]["seated"])
        self.assertTrue(seated["high"]["properties"]["seated"])


def _point(fid: str, x: float, y: float, z: float, ground: float) -> dict:
    return {
        "type": "Feature",
        "id": fid,
        "properties": {"source_ground_m": ground, "block": [0, 0], "index": 0, "placement": {}},
        "geometry": {"type": "Point", "coordinates": [x, y, z]},
    }


if __name__ == "__main__":
    unittest.main()
