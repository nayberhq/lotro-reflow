"""Seat source placements on the flow height grid."""

from __future__ import annotations

import math

from regionflow.flow import Step


def _sample_height(region, x: float, y: float) -> float | None:
    origin_x, origin_y = region.info["origin_m"]
    spacing_x, spacing_y = region.info["spacing_m"]
    gx = (x - origin_x) / spacing_x
    gy = (y - origin_y) / spacing_y
    if abs(gx - round(gx)) < 1e-8:
        gx = float(round(gx))
    if abs(gy - round(gy)) < 1e-8:
        gy = float(round(gy))
    x0 = math.floor(gx)
    y0 = math.floor(gy)
    tx = gx - x0
    ty = gy - y0
    x1 = x0 if tx == 0.0 else x0 + 1
    y1 = y0 if ty == 0.0 else y0 + 1
    total = 0.0
    for ix, iy, weight in (
        (x0, y0, (1.0 - tx) * (1.0 - ty)),
        (x1, y0, tx * (1.0 - ty)),
        (x0, y1, (1.0 - tx) * ty),
        (x1, y1, tx * ty),
    ):
        if weight == 0.0:
            continue
        code = region.sample("height", int(ix), int(iy))
        if code is None:
            return None
        total += weight * code
    return total * region.info["height_scale_m"] + region.info["height_offset_m"]


def seat_assets(region, params):
    if params:
        raise ValueError("seat_assets takes no parameters")

    def features():
        for feature in region.features("features"):
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "Point":
                continue
            coords = geometry.get("coordinates") or []
            if len(coords) < 2:
                continue
            x = float(coords[0])
            y = float(coords[1])
            z = float(coords[2]) if len(coords) > 2 else 0.0
            props = dict(feature.get("properties") or {})
            seated = {
                "type": "Feature",
                "id": feature["id"],
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [x, y, z]},
            }
            ground = _sample_height(region, x, y)
            source = props.get("source_ground_m")
            if ground is None or not isinstance(source, (int, float)) or isinstance(source, bool):
                props["seated"] = False
                yield seated
                continue
            delta = ground - float(source)
            props["ground_delta_m"] = delta
            props["seated"] = True
            seated["geometry"] = {"type": "Point", "coordinates": [x, y, z + delta]}
            yield seated

    return {"seated_assets": features()}


STEPS = {
    "seat_assets": Step(
        "seat_assets",
        "1",
        {"features": "features", "height": "u16"},
        {"seated_assets": "features"},
        "region",
        seat_assets,
    )
}
