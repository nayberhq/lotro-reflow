import json
import math
import shutil
import struct
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from regionflow.flow import Step, build
from regionflow.steps import STEPS
from regionflow.store import Region, connect

EXAMPLE = Path(__file__).resolve().parents[1] / "examples"


class FlowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        shutil.copytree(EXAMPLE, self.root / "map")
        self.db = self.root / "region.sqlite"
        self.source = self.root / "map/map.json"
        self.recipe = self.source.with_name("recipe.json")

    def edit(self, path, fn):
        data = json.loads(path.read_text())
        fn(data)
        path.write_text(json.dumps(data))

    def run_flow(self, source=True, registry=None):
        return build(self.db, self.recipe, self.source if source else None, registry)

    def view(self):
        db = connect(self.db, True)
        self.addCleanup(db.close)
        return Region(db)

    def test_whole_region_equivalence_partial_tiles_and_halo(self):
        self.run_flow()
        for size in (1, 3, 4, 16):
            self.edit(self.source, lambda d: d.update(chunk_cells=size))
            self.run_flow()
            v = self.view()
            for y in range(9):
                for x in range(10):
                    dx = (1 if x == 0 else 17 if x == 9 else 2*x) / 8
                    expected = math.degrees(math.atan(math.hypot(dx, 0.25)))
                    self.assertAlmostEqual(expected, v.sample("grade_deg", x, y), places=5)
                    self.assertEqual(v.sample("height",x,y), 80+x*x+2*y)
            self.assertEqual(v.world_xy(4,4), (108,208))
            self.assertIsNone(v.sample("height",10,3))

    def test_db_only_retry_and_recipe_removal(self):
        self.run_flow()
        self.assertEqual(set(self.run_flow().values()), {"reused"})
        self.source.unlink()
        self.assertEqual(set(self.run_flow(False).values()), {"reused"})
        self.edit(self.recipe, lambda d:d.update(steps=["intake_map"]))
        self.run_flow(False)
        self.assertEqual(set(self.view().products), {"height","paint.Road","features"})

    def test_curve_queries_on_either_side_return_one_identity(self):
        self.run_flow()
        v = self.view()
        west, = v.features("joined_curves",(100,200,108,208))
        east, = v.features("joined_curves",(116,200,118,208))
        self.assertEqual(west, east)
        self.assertEqual(west["geometry"]["coordinates"],[[118,207],[108,207],[100,206]])
        self.assertEqual(sorted(west["properties"]["source_fragments"]),["east","west"])

    def test_curve_grade_separation_and_junction(self):
        def add(c):
            def frag(fid,curve,a,b,pts):
                return dict(type="Feature", id=fid, properties={"flow_link":dict(
                    curve_id=curve,start_node=a,end_node=b)},
                    geometry=dict(type="LineString",coordinates=pts))
            # A branch sharing edge-4 splits the original chain. A deck at the
            # same XY has its own node identity and crosses without a junction.
            c["features"].extend([frag("side","spur","edge-4","yard",[[108,207],[108,210]]),
                                  frag("deck","high","A","B",[[108,207,9],[108,220,9]])])
        self.edit(self.source.with_name("curves.json"),add)
        self.run_flow()
        self.assertEqual(len(list(self.view().features("joined_curves"))),4)

    def test_closed_curve_and_order_independent_id(self):
        def loop(collection):
            edge = collection["features"][1]
            edge["properties"]["flow_link"]["start_node"] = "entrance"
            edge["geometry"]["coordinates"] = [[100,206],[105,204],[108,207]]
        path = self.source.with_name("curves.json")
        self.edit(path,loop)
        self.run_flow()
        closed, = self.view().features("joined_curves")
        self.assertEqual(closed["geometry"]["coordinates"][0],closed["geometry"]["coordinates"][-1])
        self.edit(path,lambda c:c["features"].reverse())
        self.assertEqual(self.run_flow()["stitch_curves"],"reused")
        self.assertEqual(list(self.view().features("joined_curves")),[closed])

    def test_painted_edge_mismatch(self):
        self.tile_manifest([(0,0),(1,0)])
        folder = self.source.parent
        (folder/"first.r8").write_bytes(bytes(25))
        (folder/"second.r8").write_bytes(bytes(25))
        def paint(doc):
            doc["layers"]["paint.Road"] = "u8"
            for tile,filename in zip(doc["tiles"],["first.r8","second.r8"]):
                tile["layers"]["paint.Road"] = filename
        self.edit(self.source, paint)
        self.run_flow()
        (folder/"second.r8").write_bytes(bytes([1])+bytes(24))
        with self.assertRaisesRegex(ValueError,"Seam mismatch in paint.Road"):
            self.run_flow()
        self.assertEqual(self.view().sample("paint.Road",4,0),0)

    def test_inconsistent_curve_endpoint_rolls_back_source(self):
        self.run_flow()
        self.edit(self.source.with_name("curves.json"),
                  lambda c:c["features"][0]["geometry"]["coordinates"][-1].__setitem__(0, 110))
        with self.assertRaisesRegex(ValueError,"shared curve node"):
            self.run_flow()
        self.assertEqual(self.view().sample("height",4,4),104)
        self.assertEqual(len(list(self.view().features("joined_curves"))),1)
        self.assertEqual(list(self.view().features("features"))[-1]["geometry"]["coordinates"][-1],
                         [108,207])

    def test_order_types_and_region_isolation(self):
        self.run_flow()
        self.edit(self.source, lambda d:d.update(region="Angmar"))
        with self.assertRaisesRegex(ValueError,"belongs to"):
            self.run_flow()
        bad = replace(STEPS["terrain_grade"], reads={"joined_curves":"features"})
        self.edit(self.recipe, lambda d:d.update(steps=["intake_map","terrain_grade","stitch_curves"]))
        with self.assertRaisesRegex(ValueError, "earlier step"):
            self.run_flow(False, {**STEPS,bad.name:bad})
        wrong_type = replace(bad, reads={"height":"f32"})
        with self.assertRaisesRegex(ValueError, "f32"):
            self.run_flow(False, {**STEPS,bad.name:wrong_type})
        self.assertEqual(self.view().info["region"], "synthetic-road-cut")

    def test_concurrent_writers(self):
        with ThreadPoolExecutor(2) as pool:
            outcomes = list(pool.map(lambda _: self.run_flow(), range(2)))
        self.assertEqual(sorted(r["terrain_grade"] for r in outcomes), ["built","reused"])

    def tile_manifest(self, coords):
        tiles = []
        for x,y in coords:
            buf = b"".join(struct.pack("<H",500+(4*y+j)*10+4*x+i)
                           for j in range(5) for i in range(5))
            filename = f"{x}_{y}.raw"
            self.source.with_name(filename).write_bytes(buf)
            tiles.append({"chunk":[x,y], "layers":{"height":filename}})
        def update(d):
            del d["raster"]
            d.update(layers={"height":"u16"}, tiles=tiles)
        self.edit(self.source, update)

    def test_sparse_negative_tiles_and_diagonal_only_seam(self):
        self.tile_manifest([(-1,-1),(0,0)])
        self.run_flow()
        v = self.view()
        self.assertIsNone(v.sample("height",-1,1))
        self.assertEqual(v.sample("height",-4,-4),456)
        self.assertEqual(v.sample("height",0,0),500)
        buf = self.source.with_name("0_0.raw")
        buf.write_bytes(b"\0\0" + buf.read_bytes()[2:])
        with self.assertRaisesRegex(ValueError,"Seam mismatch"):
            self.run_flow()
        self.assertEqual(self.view().sample("height",0,0),500)

    def test_derived_seam_rejected_without_publishing(self):
        self.run_flow()
        def bad(region,c,params):
            return {"bad": bytes([c.x]) * (c.w*c.h)}
        step = Step("bad","1",{"height":"u16"},{"bad":"u8"},"chunk", bad)
        self.edit(self.recipe,lambda d:d["steps"].append("bad"))
        with self.assertRaisesRegex(ValueError,"Seam mismatch in bad"):
            self.run_flow(False, {**STEPS,"bad":step})
        self.assertNotIn("bad", self.view().products)

    def test_dependent_cache_invalidation(self):
        def echo(v,p):
            return {"copy":v.features("joined_curves")}
        step = Step("copy_curves","1",{"joined_curves":"features"},{"copy":"features"},"region",echo)
        custom = {**STEPS,step.name:step}
        self.edit(self.recipe,lambda d:d["steps"].append(step.name))
        self.run_flow(registry=custom)
        self.edit(self.recipe,lambda d:d["steps"].__setitem__(1,dict(step="stitch_curves",
                                                                     params={"join_tolerance_m":0})))
        r = self.run_flow(False, custom)
        self.assertEqual((r["copy_curves"],r["terrain_grade"]),("built","reused"))

    def test_changed_source_removes_old_chunks(self):
        self.tile_manifest([(0,0),(0,1)])
        self.run_flow()
        self.edit(self.source,lambda d:d["tiles"].pop())
        self.run_flow()
        self.assertIsNone(self.view().sample("height",1,5))
        self.assertEqual(len(self.view().chunks),1)

    def test_png_matches_raw(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Optional PNG decoder unavailable")
        self.run_flow()
        folder = self.source.parent
        Image.frombytes("I;16",(10,9),(folder/"height.r16").read_bytes()).save(folder/"z.png")
        self.edit(self.source,lambda d:d["raster"]["layers"].update(height="z.png"))
        self.assertEqual(self.run_flow()["intake_map"],"reused")


if __name__ == "__main__":
    unittest.main()
