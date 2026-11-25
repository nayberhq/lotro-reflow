# LOTRO Region Flow

Build one SQLite database for each region. A recipe starts with map intake, then runs ordered Python processors over a single coordinate grid. The package runs outside Unreal. The first Unreal consumer can read the published terrain and road products from this database.

## Run the included map

Use Python 3.10 or newer. Run these commands in the directory containing this README.

```sh
python -m regionflow run demo.sqlite examples/recipe.json --source examples/map.json
python -m regionflow inspect demo.sqlite
python -m regionflow run demo.sqlite examples/recipe.json
python -m unittest discover -s tests -v
```

The synthetic fixture has 10 × 9 vertices, six chunks, a 16-bit height field, an 8-bit road mask and two oppositely digitized pieces of one source curve. The first run builds three steps. The second run reuses all three and works even if you move the source files.

| Step | Scope | Reads | Writes |
|---|---|---|---|
| `intake_map` | Region | Export manifest and source files | Grid metadata, chunk coverage, `height`, named paint grids, source `features` |
| `stitch_curves` | Region | Source `features` | `joined_curves`, with shared nodes and source fragment IDs |
| `terrain_grade` | Chunk | `height` and a one-vertex halo | `grade_deg`, the slope angle from horizontal |

The grade pass is a working reference for future raster steps. The curve pass assembles explicit source topology. Road recognition, spline fitting, terrain edits, materials and Unreal asset creation come later.

## Bring in a region

The interface to the LOTRO extractor is a versioned manifest. See `examples/map.json` for a complete single-raster example. Use a stable region identifier such as `eriador/bree-land` and a separate DB path per region. A manifest represents the complete current coverage of that region. Submitting a smaller manifest deliberately shrinks its coverage.

| Input field | Meaning |
|---|---|
| `origin_m` | World XY of integer grid vertex (0, 0), in meters |
| `spacing_m` | Positive X and Y distances between consecutive samples, in meters |
| `height_scale_m`, `height_offset_m` | Elevation in meters = raw u16 × scale + offset |
| `chunk_cells` | Edge length in **cells**. A complete chunk contains (cells + 1)² vertices. |
| `layers` | Name to sample type. `height` is u16. Painted source channels can be named individually, e.g. `paint.Road`, using u8 or u16. |
| `raster` or `tiles` | Relative filenames by layer. A whole raster supplies `shape_xy` in vertices. The importer splits it into chunks and preserves partial edge chunks. |
| `features` | Optional filename containing local world-meter Point, LineString and Polygon features in FeatureCollection JSON. Each feature needs a unique string ID. Preserve game IDs and classifications as properties. XY or XYZ coordinates are supported. |
| `provenance` | Optional JSON object for extractor version, source map ID, original axis convention or other traceability metadata. |

For existing landblock exports, replace the `raster` field with a tile list. For example, these entries represent two neighboring landblocks when `chunk_cells` matches the number of source cells along a landblock edge.

```json
"tiles": [
  {"chunk": [-1, 0], "layers": {"height": "west.r16", "paint.Road": "west-road.r8"}},
  {"chunk": [ 0, 0], "layers": {"height": "east.r16", "paint.Road": "east-road.r8"}}
]
```

Files contain row-major samples. Column index increases toward +X and row index toward +Y. Raw samples are little-endian with no header. Grayscale PNG intake supports 16-bit heights and 8-bit masks via the optional Pillow package. The importer reads a region-wide raster into memory. Use tiled input for a bounded source read size. Positive and negative chunk coordinates and sparse landblock coverage are supported. Missing chunks are outside coverage. All vertex-grid channels must share the same registration and include the duplicated far edge of each full tile.

**Extractor integration point.** Emit this manifest where the existing exporter still knows the source transform, landblock IDs and raster registration. Decode height scaling from exporter metadata. Convert flipped or rotated axes, world units, and local curve positions together. If paint is a cell-centered texture or has a different resolution, explicitly register and resample a working vertex-grid copy before submitting it. Keep the original source files and identify the conversion in provenance. This package consumes exported data and does not decode LOTRO DAT archives.

## How chunks connect

**Coordinates own the boundary.** For chunk (cx, cy), local vertex (i, j) maps to (cx × chunk_cells + i, cy × chunk_cells + j). Adjacent chunks can store the same physical vertex twice. Intake compares every shared edge and corner byte for byte on every layer, including corners where only diagonal neighbors exist. A mismatched sample rejects the build with product name and both chunk coordinates. Derived raster outputs pass through the same check before publication.

**A processor reads its neighborhood and writes its core.** `region.window("height", chunk, halo=1)` returns rows with one additional sample on every side, pulled through the regional grid. Missing coverage is represented by `None`. The processor chooses its boundary condition. Terrain grade uses centered differences where both neighbors exist and one-sided differences at coverage edges. For a larger kernel, request the full kernel radius. Global coordinate inputs, including any procedural noise seeds, must be independent of chunk ID. Shared-edge equality catches cracks, while a whole-region comparison also catches blend or derivative errors inside a chunk.

**A feature belongs to the region.** Query `region.features("joined_curves", region.bounds_m(chunk))` to retrieve complete features whose world-space bounding boxes intersect the chunk. Two chunks can retrieve the same curve and ID. This is a conservative spatial query. Test the actual geometry if exact intersection is required. Emit one Unreal spline per regional ID or derive disposable chunk sections from that curve. Do not spawn an entire spline once per query result.

**Joining fragmented curves.** The manifest may attach explicit connectivity to a LineString:

```json
"properties": {
  "flow_link": {
    "curve_id": "source-route-42",
    "start_node": "route-42-entry",
    "end_node": "route-42-crossing-17"
  }
}
```

Node IDs are region-global. Allocate different IDs for grade-separated crossings. The stitcher joins degree-two chains within a curve, handles reversed fragments and closed loops, and splits output at junctions. All endpoints claiming a node must agree within `join_tolerance_m` (default 0.001 m). The canonical endpoint is selected deterministically. Traversal is topological and does not preserve a traffic direction. The untouched source features preserve original orientation and properties. Joined feature IDs are stable across input order and raster rechunking with unchanged fragment IDs.

Supply an exported shared node ID whenever one exists. If an extractor clips an already-known curve into landblocks, it can assign the cut node from the parent curve ID and original along-curve position. With only paint or unclassified fragments and no reliable endpoints, retain them as evidence for `resolve_roads`. The stitcher leaves untagged features in the source collection and does not guess connectivity from XY proximity.

## Recipe and extension contract

Recipe order is explicit. Each product has one writer and a declared kind (`u8`, `u16`, `f32` or `features`). Missing inputs, type mismatches, duplicate producers and invalid ordering fail before publication. Use either a step name or a settings object:

```json
{
  "schema": 1,
  "steps": [
    "intake_map",
    {"step": "stitch_curves", "params": {"join_tolerance_m": 0.01}},
    "terrain_grade"
  ]
}
```

Register a trusted Python module through `--plugin module_name`. Its `STEPS` dictionary maps names to `regionflow.flow.Step` declarations. A region callback receives `(region, params)` and returns named feature iterables. A chunk callback receives `(region, chunk, params)` and returns named little-endian grid byte strings covering the chunk, including its shared edges. See `regionflow/steps.py` and `regionflow/curves.py` for working implementations. Declare every product read and bump the step version whenever its implementation or an external dependency changes. A reproducible callback uses only declared products, region metadata and recipe parameters.

**Publication and reuse.** One transaction publishes the complete requested recipe. A failed intake, processor, or seam check leaves the last successful snapshot readable. Writers to one region serialize through SQLite. Independent region files can build concurrently. Running with `--source` reads and validates the source, compares content signatures, and reuses identical input. Omit it for fast database-only iteration. A changed source replaces the region snapshot and rebuilds selected derived products. With unchanged source, step versions, settings, layout and upstream signatures determine reuse. Removing a recipe step retires its products on the next successful run. V1 rebuilds a dirty step across the region; it has no distributed scheduler or per-chunk invalidation.

## First Bree-land integration

1. Export two neighboring landblocks crossed by one road. Record a known world position, a road point and an elevation near the shared edge.
2. Produce the manifest and compare the database coordinates and elevation with those three source references. Confirm shared paint and height edges pass.
3. Build `resolve_roads` as a region processor consuming paint, curves, heights and any available placement or water evidence. Give each road a stable ID and junctions. Where source connectivity is uncertain, preserve that uncertainty.
4. Add chunk processors for terrain cuts and material weights. They can read complete regional roads and local raster neighborhoods. Bake an Unreal Landscape only after the regional results pass validation.

The synthetic acceptance test compares every slope vertex against the expected whole-region calculation over four chunk sizes. Real source registration, visual fidelity and Unreal placement require the first real crop.
