"""Reassemble source curve fragments with explicit topology in region space."""
from collections import defaultdict
from itertools import combinations
from math import dist, isfinite

from .intake import signature


def stitch_curves(region, params):
    tolerance = params.get("join_tolerance_m", 0.001)
    if params.keys() - {"join_tolerance_m"} or type(tolerance) not in (int,float) or not (
        isfinite(tolerance) and tolerance >= 0
    ):
        raise ValueError("join_tolerance_m must be finite and nonnegative")
    endpoints = defaultdict(list)
    adjacency = defaultdict(list)
    fragments = {}
    degree = defaultdict(int)
    for f in region.features("features"):
        link = (f.get("properties") or {}).get("flow_link")
        if link is None:
            continue
        curve, start, end = (link[k] for k in ("curve_id", "start_node", "end_node"))
        if not all(isinstance(v, str) and v for v in (curve, start, end)):
            raise ValueError("flow_link identifiers must be nonempty strings")
        if f["geometry"]["type"] != "LineString":
            raise ValueError(f"Expected a curve fragment for {f['id']}")
        coords = f["geometry"]["coordinates"]
        fragments[f["id"]] = (curve, start, end, coords)
        for node, p in ((start, coords[0]), (end, coords[-1])):
            endpoints[node].append(p)
            adjacency[curve,node].append(f["id"])
            degree[node] += 1
    points = {}
    for node, positions in endpoints.items():
        if any(len(a) != len(b) or dist(a,b) > tolerance for a,b in combinations(positions,2)):
            raise ValueError(f"Conflicting positions for shared curve node {node}")
        points[node] = min(positions)
    pending = set(fragments)

    def walk(curve, start, fragment):
        node = start
        line = []
        members = []
        while fragment in pending:
            pending.remove(fragment)
            members.append(fragment)
            _, a, b, coords = fragments[fragment]
            forward = node == a
            next_node = b if forward else a
            middle = coords[1:-1] if forward else list(reversed(coords[1:-1]))
            if not line:
                line.append(points[node])
            line.extend(middle)
            line.append(points[next_node])
            node = next_node
            links = adjacency[curve,node]
            if degree[node] != 2 or len(links) != 2:
                break
            fragment = links[1] if links[0] == fragment else links[0]
        return {"type":"Feature", "id":"curve-" + signature([curve, sorted(members)]),
                "properties":{"curve_id":curve, "start_node":start, "end_node":node,
                              "source_fragments":members},
                "geometry":{"type":"LineString", "coordinates":line}}

    def lines():
        for (curve,node), ids in sorted(adjacency.items()):
            if degree[node] == len(ids) == 2:
                continue
            for fid in sorted(ids):
                if fid in pending:
                    yield walk(curve, node, fid)
        # Remaining connected components are closed loops.
        for fid in sorted(pending):
            if fid in pending:
                curve, start, end, _ = fragments[fid]
                yield walk(curve, min(start,end), fid)
    return {"joined_curves": lines()}
