import argparse
import importlib
import json
import sqlite3
import sys
from contextlib import closing

from .flow import build
from .steps import STEPS
from .store import check_schema, connect, meta


def main():
    p = argparse.ArgumentParser(description="Regional terrain flow")
    sub = p.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("db")
    run.add_argument("recipe")
    run.add_argument("--source", help="map manifest; omit to run from the database")
    run.add_argument("--plugin", action="append", default=[], help="trusted Python module exporting STEPS")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("db")
    args = p.parse_args()
    if args.cmd == "run":
        registry = dict(STEPS)
        for module in args.plugin:
            extra = importlib.import_module(module).STEPS
            if registry.keys() & extra.keys() or "intake_map" in extra:
                raise ValueError(f"Duplicate registrations in {module}")
            registry.update(extra)
        result = build(args.db, args.recipe, args.source, registry)
    else:
        with closing(connect(args.db, readonly=True)) as db:
            db.execute("BEGIN")
            check_schema(db)
            result = {"region":meta(db), "chunks":db.execute("SELECT count(*) FROM chunks").fetchone()[0],
                      "products":[dict(zip(("name","kind","step","signature"),r)) for r in db.execute(
                          "SELECT name,kind,producer,signature FROM products ORDER BY name")]}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, ImportError, sqlite3.Error) as exc:
        sys.exit(f"regionflow: {exc}")
