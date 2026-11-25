"""A real cross-boundary raster step, small enough to serve as a plugin example."""
import math

from .curves import stitch_curves
from .flow import Step
from .store import FORMATS


def terrain_grade(region, chunk, params):
    if params:
        raise ValueError("terrain_grade takes no parameters")
    sx, sy = region.info["spacing_m"]
    scale = region.info["height_scale_m"]
    out = bytearray(chunk.w*chunk.h*4)
    heights = region.window("height", chunk, halo=1)

    def derivative(before, center, after, spacing):
        if before is None and after is None:
            return 0.0
        if before is None:
            return (after-center)*scale/spacing
        if after is None:
            return (center-before)*scale/spacing
        return (after-before)*scale/(2*spacing)

    for row in range(chunk.h):
        previous, current, following = heights[row:row+3]
        for col in range(chunk.w):
            i = col + 1
            z = current[i]
            dx = derivative(current[i-1], z, current[i+1], sx)
            dy = derivative(previous[i], z, following[i], sy)
            angle = math.degrees(math.atan(math.hypot(dx,dy)))
            FORMATS["f32"].pack_into(out, 4*(row*chunk.w+col), angle)
    return {"grade_deg": bytes(out)}


grade = Step("terrain_grade", "1", {"height":"u16"}, {"grade_deg":"f32"}, "chunk", terrain_grade)
curves = Step("stitch_curves", "1", {"features":"features"}, {"joined_curves":"features"},
              "region", stitch_curves)
STEPS = {step.name: step for step in (curves, grade)}
