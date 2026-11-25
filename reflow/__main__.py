"""Build the bree_ground window and publish it for the Unreal commandlet."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from regionflow.flow import build
from regionflow.steps import STEPS

from reflow.manifest import WINDOW, write_manifest
from reflow.publish import publish
from reflow.seat import STEPS as SEAT_STEPS

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXPORT = Path("/mnt/c/dev/lotro_reforge/Exports/bree")
DEFAULT_PUBLISH = Path("/mnt/c/dev/lotro_reforge/Exports/bree_reflow")


def bree_ground(export: Path, publish_dir: Path, build_root: Path) -> dict:
    source_dir = build_root / "bree_ground"
    source_dir.mkdir(parents=True, exist_ok=True)
    map_path = write_manifest(export, source_dir, WINDOW)
    database = build_root / "bree_ground.sqlite"
    recipe = ROOT / "recipes" / "bree_ground.json"
    registry = dict(STEPS)
    registry.update(SEAT_STEPS)
    report = build(str(database), str(recipe), str(map_path), registry)
    report["publish"] = publish(database, export, publish_dir)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Build bree_ground through Region Flow")
    sub = parser.add_subparsers(dest="cmd", required=True)
    command = sub.add_parser("bree-ground")
    command.add_argument("--export", type=Path, default=DEFAULT_EXPORT)
    command.add_argument("--publish", type=Path, default=DEFAULT_PUBLISH)
    command.add_argument("--build", type=Path, default=ROOT / "build")
    args = parser.parse_args()
    if args.cmd == "bree-ground":
        print(json.dumps(bree_ground(args.export, args.publish, args.build), indent=2))


if __name__ == "__main__":
    main()
