#!/usr/bin/env python3
"""Build a simulation seed manifest with an even spread of defect cases.

The v1.5 source data concentrates every defect in two cells: all 500 defective
RGB frames belong to SIM-0001 and SIM-0002, and the only three defective CT
frames belong to SIM-0001. A manifest that gives each cell nothing but its own
images therefore cannot produce a varied run, and the first manifest went the
other way and gave every cell the same defective RGB set, which made all twenty
cells reject identically.

This assigns images across cells on purpose. The loader permits it because its
uniqueness rule is (battery_cell_id, bucket_name, object_key), so the same
object may serve several cells as long as it reaches each of them once.

The output satisfies the loader's validator: 880 rows, 20 INITIAL images per
cell and modality, 20 RECAPTURE images for each of the four fixed recapture
groups and none elsewhere, one quality FAIL inside the INITIAL set of exactly
those four groups, and PASS everywhere else.

Usage:
    python scripts/build-simulation-manifest.py --download -o manifest.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

BUCKET = "kt-aivle-big-proj-kks"
PREFIX = "simulations/server-simulation-v1.5"
REGION = "ap-northeast-2"
CELLS = [f"SIM-{index:04d}" for index in range(1, 21)]
RECAPTURE_GROUPS = {
    ("SIM-0001", "CT"),
    ("SIM-0018", "CT"),
    ("SIM-0002", "RGB"),
    ("SIM-0016", "RGB"),
}
EXT = {"CT": ".jpg", "RGB": ".png"}

# The label archives this reads. The JSON carries defect type names; the
# recapture sets ship YOLO labels only, which still separates a clean frame
# from a defective one.
ARCHIVES = (
    "initial_CT_json.zip",
    "initial_RGB_json.zip",
    "recapture_CT_labels_det.zip",
    "recapture_RGB_labels_det.zip",
)

# How many of a cell's 20 INITIAL images carry a defect, per modality. Index
# 0..19 lines up with SIM-0001..SIM-0020, and zero means a clean cell. Five
# cells are clean in both, five reject on RGB only, five on CT only, and five on
# both, with the density stepped so a run crosses more than one decision point.
#
# CT density stops at three because the source holds exactly three defective CT
# frames. Asking for more cannot be satisfied from v1.5 at all.
CT_DEFECTS = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 2, 3, 1, 2, 1, 2, 3, 1, 2]
RGB_DEFECTS = [0, 0, 0, 0, 0, 1, 2, 3, 5, 10, 0, 0, 0, 0, 0, 1, 2, 3, 5, 10]

# "Damaged" exists on only three RGB frames and sorting by filename never
# reaches them, so the rare type has to be placed deliberately. These cells give
# up one Pollution frame for a Damaged one, which covers the type without
# disturbing the density plan above.
DAMAGED_CELLS = {"SIM-0008", "SIM-0017", "SIM-0020"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-o", "--output", type=Path, required=True,
        help="path to write the manifest CSV to",
    )
    parser.add_argument(
        "--labels-dir", type=Path, default=Path(".cache/simulation-labels"),
        help="directory holding the label archives",
    )
    parser.add_argument(
        "--download", action="store_true",
        help="fetch any missing label archive from S3 before building",
    )
    return parser.parse_args()


def ensure_archives(labels_dir: Path, download: bool) -> None:
    labels_dir.mkdir(parents=True, exist_ok=True)
    missing = [name for name in ARCHIVES if not (labels_dir / name).is_file()]
    if not missing:
        return
    if not download:
        raise SystemExit(
            f"missing label archives in {labels_dir}: {', '.join(missing)}\n"
            "re-run with --download, or copy them from "
            f"s3://{BUCKET}/{PREFIX}/metadata-zips/"
        )
    for name in missing:
        print(f"downloading {name}", file=sys.stderr)
        subprocess.run(
            ["aws", "s3", "cp", f"s3://{BUCKET}/{PREFIX}/metadata-zips/{name}",
             str(labels_dir / name), "--region", REGION, "--only-show-errors"],
            check=True,
        )


def load_labels(labels_dir: Path) -> dict[str, list[str]]:
    """image stem -> defect type names, empty when the frame is clean"""
    meta: dict[str, list[str]] = {}

    for name in ("initial_CT_json.zip", "initial_RGB_json.zip"):
        with zipfile.ZipFile(labels_dir / name) as archive:
            for entry in archive.namelist():
                if not entry.endswith(".json"):
                    continue
                document = json.loads(archive.read(entry))
                defects = document.get("defects") or []
                meta[Path(entry).stem] = sorted({d.get("name") for d in defects})

    for name in ("recapture_CT_labels_det.zip", "recapture_RGB_labels_det.zip"):
        with zipfile.ZipFile(labels_dir / name) as archive:
            for entry in archive.namelist():
                if not entry.endswith(".txt"):
                    continue
                body = archive.read(entry).decode("utf-8", "replace").strip()
                meta[Path(entry).stem] = ["(defect)"] if body else []

    return meta


def object_key(stem: str, capture_set: str, image_type: str) -> str:
    return f"{PREFIX}/{capture_set.lower()}/{image_type.lower()}/{stem}{EXT[image_type]}"


def build_pools(meta: dict[str, list[str]]) -> dict[tuple[str, str, bool], list[str]]:
    """(capture set, image type, defective) -> sorted stems"""
    pools: dict[tuple[str, str, bool], list[str]] = defaultdict(list)
    for stem, defects in meta.items():
        parts = stem.split("_")
        capture_set, image_type = parts[0].upper(), parts[1]
        if image_type not in EXT:
            continue
        pools[(capture_set, image_type, bool(defects))].append(stem)
    for key in pools:
        pools[key].sort()
    return pools


def take(
    pool: list[str],
    used: set[tuple[str, str]],
    count: int,
    cell: str,
    offset: int = 0,
) -> list[str]:
    """Pick `count` stems, starting `offset` into the pool and wrapping.

    The offset is what stops every cell from receiving the same frames. Clean
    pools hold thousands of images, so each cell gets its own slice. Defect
    pools hold as few as three, so there reuse across cells is unavoidable.
    """
    if not pool:
        raise RuntimeError(f"empty pool for {cell}")

    chosen: list[str] = []
    size = len(pool)
    for step in range(size):
        if len(chosen) == count:
            break
        stem = pool[(offset + step) % size]
        if (cell, stem) in used:
            continue
        chosen.append(stem)
        used.add((cell, stem))
    if len(chosen) < count:
        raise RuntimeError(
            f"pool exhausted for {cell}: wanted {count}, got {len(chosen)}"
        )
    return chosen


def build(meta: dict[str, list[str]]) -> list[dict[str, str]]:
    pools = build_pools(meta)
    damaged = sorted(
        stem for stem, defects in meta.items()
        if stem.startswith("initial_RGB_") and "Damaged" in defects
    )
    used: set[tuple[str, str]] = set()
    rows: list[dict[str, str]] = []

    for index, cell in enumerate(CELLS):
        for image_type, plan in (("CT", CT_DEFECTS), ("RGB", RGB_DEFECTS)):
            defect_count = plan[index]

            stems: list[str] = []
            remaining = defect_count
            if image_type == "RGB" and cell in DAMAGED_CELLS and remaining:
                stems += take(damaged, used, 1, cell, offset=index)
                remaining -= 1
            stems += take(
                pools[("INITIAL", image_type, True)],
                used, remaining, cell, offset=index * 7,
            )
            stems += take(
                pools[("INITIAL", image_type, False)],
                used, 20 - defect_count, cell, offset=index * 37,
            )

            # The four recapture groups need one quality FAIL in their INITIAL
            # set; every other INITIAL group must be entirely PASS.
            fail_stem = stems[0] if (cell, image_type) in RECAPTURE_GROUPS else None

            for stem in stems:
                rows.append({
                    "cell_serial_no": cell,
                    "image_type": image_type,
                    "capture_set": "INITIAL",
                    "bucket_name": BUCKET,
                    "object_key": object_key(stem, "INITIAL", image_type),
                    "quality_label": "FAIL" if stem == fail_stem else "PASS",
                })

            if (cell, image_type) in RECAPTURE_GROUPS:
                # A retake is what the line produces after a bad capture, so it
                # comes from the clean recapture pool whichever cell shot it.
                for stem in take(
                    pools[("RECAPTURE", image_type, False)],
                    used, 20, cell, offset=index * 23,
                ):
                    rows.append({
                        "cell_serial_no": cell,
                        "image_type": image_type,
                        "capture_set": "RECAPTURE",
                        "bucket_name": BUCKET,
                        "object_key": object_key(stem, "RECAPTURE", image_type),
                        "quality_label": "PASS",
                    })

    return rows


def report(meta: dict[str, list[str]], rows: list[dict[str, str]]) -> None:
    composition: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in rows:
        if row["capture_set"] != "INITIAL":
            continue
        defects = meta.get(Path(row["object_key"]).stem) or ["clean"]
        for name in defects:
            composition[(row["cell_serial_no"], row["image_type"])][name] += 1

    print(f"{'cell':10}{'CT defects':20}{'RGB defects':28}expected")
    for cell in CELLS:
        summary = {}
        for image_type in ("CT", "RGB"):
            counts = composition[(cell, image_type)]
            summary[image_type] = ", ".join(
                f"{name}:{count}"
                for name, count in sorted(counts.items())
                if name != "clean"
            ) or "-"
        rejects = {
            image_type: summary[image_type] != "-" for image_type in ("CT", "RGB")
        }
        verdict = " / ".join(
            f"{image_type} {'REJECT' if rejects[image_type] else 'PASS'}"
            for image_type in ("CT", "RGB")
        )
        print(f"{cell:10}{summary['CT']:20}{summary['RGB']:28}{verdict}")


def main() -> None:
    args = parse_args()
    ensure_archives(args.labels_dir, args.download)
    meta = load_labels(args.labels_dir)
    rows = build(meta)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "cell_serial_no",
                "image_type",
                "capture_set",
                "bucket_name",
                "object_key",
                "quality_label",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)

    report(meta, rows)
    print(f"\nwrote {args.output} ({len(rows)} rows)")


if __name__ == "__main__":
    main()
