"""Ordered recipes with typed products and atomic publication."""
from contextlib import closing
from dataclasses import dataclass
from typing import Callable

from .intake import intake, load_json, signature
from .store import FORMATS, Region, check_schema, connect, meta, put_feature, verify_glue


@dataclass(frozen=True)
class Step:
    name: str
    version: str
    reads: dict[str, str]
    writes: dict[str, str]
    scope: str  # chunk callbacks return packed grids, region callbacks return feature iterables
    compute: Callable


def recipe_steps(recipe, registry):
    if recipe["schema"] != 1:
        raise ValueError("Unsupported recipe version")
    entries = [(e, {}) if isinstance(e, str) else (e["step"], e.get("params", {}))
               for e in recipe["steps"]]
    names = [name for name, _ in entries]
    if names[:1] != ["intake_map"] or len(names) != len(set(names)):
        raise ValueError("Start with intake_map and include each step at most once")
    if entries[0][1]:
        raise ValueError("Intake settings belong in the map manifest")
    for name, params in entries[1:]:
        if name not in registry or not isinstance(params, dict):
            raise ValueError(f"Unknown step or invalid parameters: {name}")
    return entries


def execute_step(db, step, params, fingerprint):
    for product, kind in step.writes.items():
        db.execute("INSERT INTO products VALUES(?,?,?,?)",
                   (product, kind, step.name, fingerprint))
    region = Region(db)
    region.products = dict(step.reads)
    if step.scope == "chunk":
        for c in region.chunks.values():
            result = step.compute(region, c, params)
            if result.keys() != step.writes.keys():
                raise ValueError(f"{step.name} returned different products than declared")
            for name, data in result.items():
                if not isinstance(data, bytes) or len(data) != c.w*c.h*FORMATS[step.writes[name]].size:
                    raise ValueError(f"Invalid {name} payload at {(c.x, c.y)}")
                db.execute("INSERT INTO tiles VALUES(?,?,?,?)", (name,c.x,c.y,data))
        result_view = Region(db)
        for name in step.writes:
            verify_glue(result_view, name)
    else:
        result = step.compute(region, params)
        if result.keys() != step.writes.keys():
            raise ValueError(f"{step.name} returned different products than declared")
        for name, features in result.items():
            for feature in features:
                put_feature(db, name, feature)


def build(path, recipe_path, source=None, registry=None):
    from .steps import STEPS
    registry = STEPS if registry is None else registry
    entries = recipe_steps(load_json(recipe_path), registry)
    report = {}
    with closing(connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        check_schema(db, create=True)
        if source:
            report["intake_map"] = intake(db, source)
        elif meta(db):
            report["intake_map"] = "reused"
        else:
            raise ValueError("First build requires a source manifest")
        available = dict(db.execute("SELECT name,kind FROM products WHERE producer='intake_map'"))
        for name, _ in entries[1:]:
            step = registry[name]
            if step.name != name or step.scope not in {"chunk", "region"} or not step.writes:
                raise ValueError(f"Invalid step declaration: {name}")
            allowed = FORMATS if step.scope == "chunk" else {"features"}
            if any(v not in allowed for v in step.writes.values()):
                raise ValueError(f"Invalid output type for {name}")
            for product, kind in step.reads.items():
                if available.get(product) != kind:
                    raise ValueError(f"{name} needs {product} ({kind}) from an earlier step")
            if available.keys() & step.writes.keys():
                raise ValueError(f"{name} attempts to overwrite an existing product")
            available.update(step.writes)

        selected = {name for name, _ in entries}
        for (name,) in list(db.execute("SELECT DISTINCT producer FROM products")):
            if name not in selected:
                db.execute("DELETE FROM products WHERE producer=?", (name,))
        # Retire any previous declarations before building. A recipe can move a
        # product to another producer as long as the new graph has one writer.
        for product, owner in list(db.execute("SELECT name,producer FROM products")):
            if owner != "intake_map" and product not in registry[owner].writes:
                db.execute("DELETE FROM products WHERE name=?", (product,))
        for name, params in entries[1:]:
            step = registry[name]
            signatures = dict(db.execute("SELECT name,signature FROM products"))
            fingerprint = signature([name, step.version, step.scope, params, step.writes, step.reads,
                                     {p: signatures[p] for p in step.reads},
                                     dict(db.execute("SELECT key,value FROM meta")),
                                     list(db.execute("SELECT * FROM chunks ORDER BY x,y"))])
            expected = {key: (kind, fingerprint) for key,kind in step.writes.items()}
            existing = {key:(kind,sig) for key,kind,sig in db.execute(
                "SELECT name,kind,signature FROM products WHERE producer=?", (name,))}
            if existing == expected:
                report[name] = "reused"
                continue
            db.execute("DELETE FROM products WHERE producer=?", (name,))
            execute_step(db, step, params, fingerprint)
            report[name] = "built"
    return report
