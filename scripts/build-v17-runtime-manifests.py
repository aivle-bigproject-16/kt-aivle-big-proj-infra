#!/usr/bin/env python3
"""Build five deterministic 20-cell runtime waves from a v1.7 dataset.

The database contains 20 physical SIM cells. This script maps 100 logical QA
cases onto those cells in five waves, selecting 40 CT and 40 RGB INITIAL images
per case. Capture-failure cases also receive the exact 40 linked RECAPTURE
images. Full 40-image sets are never reused.
"""

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


BUCKET = "kt-aivle-big-proj-kks"
PREFIX = "simulations/server-simulation-v1.7"
SEED = 20260816
CASES = 100
CASES_PER_WAVE = 20
IMAGES_PER_MODALITY = 40
CT_AXIS_PLAN = {"x": 5, "y": 17, "z": 18}
CT_POROSITY_IMAGES_PER_DEFECT_CASE = 3
FAIL_IMAGES_PER_CAPTURE_CASE = 9
CT_DEFECT_CASES = set(range(1, 6))
RGB_DEFECT_CASES = set(range(6, 16))
CT_CAPTURE_FAIL_CASES = set(range(16, 26))
RGB_CAPTURE_FAIL_CASES = set(range(26, 36))
OUTPUT_FIELDS = [
    "wave_no",
    "logical_case_id",
    "cell_serial_no",
    "image_type",
    "capture_set",
    "bucket_name",
    "object_key",
    "quality_label",
    "product_status",
    "source_sample_id",
    "retry_of_sample_id",
    "output_battery_id",
    "original_battery_id",
    "axis",
    "source_image_sha256",
    "has_porosity",
    "has_damaged",
    "has_pollution",
]


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("dataset_manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def load_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {
        "sample_id",
        "capture_set",
        "retry_of_sample_id",
        "modality",
        "product_status",
        "axis",
        "capture_quality",
        "output_image_path",
        "output_image_sha256",
        "output_battery_id",
        "original_battery_id",
        "source_image_sha256",
        "has_porosity",
        "has_damaged",
        "has_pollution",
    }
    missing = required - set(rows[0] if rows else ())
    if missing:
        raise ValueError(f"dataset manifest is missing fields: {sorted(missing)}")
    return rows


class Selector:
    def __init__(self, rows, globally_used):
        self.rows = list(rows)
        random.Random(SEED).shuffle(self.rows)
        self.cursor = 0
        self.globally_used = globally_used

    def take(self, count, forbidden):
        chosen = []
        for allow_reuse in (False, True):
            visited = 0
            while len(chosen) < count and visited < len(self.rows):
                row = self.rows[self.cursor % len(self.rows)]
                self.cursor += 1
                visited += 1
                sample_id = row["sample_id"]
                if sample_id in forbidden:
                    continue
                if not allow_reuse and self.globally_used[sample_id]:
                    continue
                chosen.append(row)
                forbidden.add(sample_id)
                self.globally_used[sample_id] += 1
            if len(chosen) == count:
                return chosen
        raise RuntimeError(
            f"pool exhausted: wanted {count}, selected {len(chosen)}, "
            f"pool size {len(self.rows)}"
        )


def pool_key(row, linked_only, annotated_defect_only=False):
    return (
        row["modality"],
        row["product_status"],
        row["capture_quality"],
        row["axis"],
        linked_only,
        annotated_defect_only,
    )


def build_selectors(initial_rows, linked_ids):
    pools = defaultdict(list)
    globally_used = Counter()
    for row in initial_rows:
        pools[pool_key(row, False)].append(row)
        if row["sample_id"] in linked_ids:
            pools[pool_key(row, True)].append(row)
        if row["modality"] == "CT" and row["has_porosity"] == "1":
            pools[pool_key(row, False, True)].append(row)
            if row["sample_id"] in linked_ids:
                pools[pool_key(row, True, True)].append(row)
    return {
        key: Selector(rows, globally_used)
        for key, rows in pools.items()
    }


def select(
    selectors,
    modality,
    product_status,
    quality,
    axis,
    count,
    linked_only,
    forbidden,
    annotated_defect_only=False,
):
    key = (
        modality,
        product_status,
        quality,
        axis,
        linked_only,
        annotated_defect_only,
    )
    selector = selectors.get(key)
    if selector is None:
        raise RuntimeError(f"missing source pool: {key}")
    return selector.take(count, forbidden)


def choose_initial(
    selectors,
    case_no,
    modality,
    product_status,
    capture_fail,
):
    forbidden = set()
    selected = []
    if modality == "CT":
        fail_by_axis = {"x": 0, "y": 4, "z": 5}
        for axis, total in CT_AXIS_PLAN.items():
            fail_count = fail_by_axis[axis] if capture_fail else 0
            annotated_count = (
                CT_POROSITY_IMAGES_PER_DEFECT_CASE
                if product_status == "defective" and axis == "y"
                else 0
            )
            selected += select(
                selectors,
                modality,
                product_status,
                "FAIL",
                axis,
                fail_count,
                True,
                forbidden,
            ) if fail_count else []
            selected += select(
                selectors,
                modality,
                product_status,
                "PASS",
                axis,
                annotated_count,
                capture_fail,
                forbidden,
                annotated_defect_only=True,
            ) if annotated_count else []
            selected += select(
                selectors,
                modality,
                product_status,
                "PASS",
                axis,
                total - fail_count - annotated_count,
                capture_fail,
                forbidden,
            )
    else:
        fail_count = FAIL_IMAGES_PER_CAPTURE_CASE if capture_fail else 0
        selected += select(
            selectors,
            modality,
            product_status,
            "FAIL",
            "",
            fail_count,
            True,
            forbidden,
        ) if fail_count else []
        selected += select(
            selectors,
            modality,
            product_status,
            "PASS",
            "",
            IMAGES_PER_MODALITY - fail_count,
            capture_fail,
            forbidden,
        )

    if len(selected) != IMAGES_PER_MODALITY:
        raise AssertionError(f"case {case_no} {modality} did not select 40")
    if modality == "CT":
        porosity_count = sum(row["has_porosity"] == "1" for row in selected)
        expected = (
            CT_POROSITY_IMAGES_PER_DEFECT_CASE
            if product_status == "defective"
            else 0
        )
        if porosity_count != expected:
            raise AssertionError(
                f"case {case_no} CT expected {expected} porosity images, "
                f"selected {porosity_count}"
            )
    return sorted(
        selected,
        key=lambda row: (row["axis"], int(row["output_sequence_order"])),
    )


def runtime_row(
    source,
    wave_no,
    logical_case_id,
    cell_serial_no,
    capture_set,
):
    return {
        "wave_no": str(wave_no),
        "logical_case_id": logical_case_id,
        "cell_serial_no": cell_serial_no,
        "image_type": source["modality"],
        "capture_set": capture_set,
        "bucket_name": BUCKET,
        "object_key": f"{PREFIX}/{source['output_image_path']}",
        "quality_label": source["capture_quality"],
        "product_status": source["product_status"],
        "source_sample_id": source["sample_id"],
        "retry_of_sample_id": source["retry_of_sample_id"],
        "output_battery_id": source["output_battery_id"],
        "original_battery_id": source["original_battery_id"],
        "axis": source["axis"],
        "source_image_sha256": source["output_image_sha256"],
        "has_porosity": source["has_porosity"],
        "has_damaged": source["has_damaged"],
        "has_pollution": source["has_pollution"],
    }


def build(rows):
    initial_rows = [
        row for row in rows if row["capture_set"] == "initial_capture"
    ]
    recapture_by_initial = {
        row["retry_of_sample_id"]: row
        for row in rows
        if row["capture_set"] == "recapture"
    }
    selectors = build_selectors(initial_rows, set(recapture_by_initial))
    waves = defaultdict(list)
    fingerprints = set()
    audit_cases = []

    for case_no in range(1, CASES + 1):
        wave_no = (case_no - 1) // CASES_PER_WAVE + 1
        cell_no = (case_no - 1) % CASES_PER_WAVE + 1
        logical_case_id = f"QA-{case_no:04d}"
        cell_serial_no = f"SIM-{cell_no:04d}"
        case_fingerprint_parts = []

        for modality in ("CT", "RGB"):
            defect_cases = (
                CT_DEFECT_CASES if modality == "CT" else RGB_DEFECT_CASES
            )
            fail_cases = (
                CT_CAPTURE_FAIL_CASES
                if modality == "CT"
                else RGB_CAPTURE_FAIL_CASES
            )
            product_status = (
                "defective" if case_no in defect_cases else "normal"
            )
            capture_fail = case_no in fail_cases
            selected = choose_initial(
                selectors,
                case_no,
                modality,
                product_status,
                capture_fail,
            )
            case_fingerprint_parts += [row["sample_id"] for row in selected]
            waves[wave_no] += [
                runtime_row(
                    row,
                    wave_no,
                    logical_case_id,
                    cell_serial_no,
                    "INITIAL",
                )
                for row in selected
            ]

            if capture_fail:
                recaptures = [
                    recapture_by_initial[row["sample_id"]]
                    for row in selected
                ]
                waves[wave_no] += [
                    runtime_row(
                        row,
                        wave_no,
                        logical_case_id,
                        cell_serial_no,
                        "RECAPTURE",
                    )
                    for row in recaptures
                ]

            audit_cases.append({
                "logicalCaseId": logical_case_id,
                "waveNo": wave_no,
                "cellSerialNo": cell_serial_no,
                "modality": modality,
                "productStatus": product_status,
                "captureFailureTarget": capture_fail,
                "qualityFailCount": sum(
                    row["capture_quality"] == "FAIL" for row in selected
                ),
                "annotatedDefectImageCount": sum(
                    row[flag] == "1"
                    for row in selected
                    for flag in (
                        ("has_porosity",)
                        if modality == "CT"
                        else ("has_damaged", "has_pollution")
                    )
                ),
                "originalBatteryIds": sorted({
                    row["original_battery_id"] for row in selected
                }),
                "outputBatteryIds": sorted({
                    row["output_battery_id"] for row in selected
                }),
            })

        fingerprint = hashlib.sha256(
            "\n".join(sorted(case_fingerprint_parts)).encode()
        ).hexdigest()
        if fingerprint in fingerprints:
            raise RuntimeError(f"duplicate full image set: {logical_case_id}")
        fingerprints.add(fingerprint)
        for item in audit_cases[-2:]:
            item["caseFingerprint"] = fingerprint

    reuse = Counter()
    for wave_rows in waves.values():
        for row in wave_rows:
            if row["capture_set"] == "INITIAL":
                reuse[row["source_sample_id"]] += 1

    audit = {
        "schemaVersion": 1,
        "generatorSeed": SEED,
        "caseCount": CASES,
        "waveCount": len(waves),
        "imagesPerModality": IMAGES_PER_MODALITY,
        "minimumValidCoverage": 0.8,
        "ctAxisPlan": CT_AXIS_PLAN,
        "ctPorosityImagesPerDefectCase": (
            CT_POROSITY_IMAGES_PER_DEFECT_CASE
        ),
        "targets": {
            "ctDefectiveCases": len(CT_DEFECT_CASES),
            "rgbDefectiveCases": len(RGB_DEFECT_CASES),
            "ctCaptureFailCases": len(CT_CAPTURE_FAIL_CASES),
            "rgbCaptureFailCases": len(RGB_CAPTURE_FAIL_CASES),
        },
        "uniqueInitialSourceRows": len(reuse),
        "reusedInitialSourceRows": sum(count > 1 for count in reuse.values()),
        "maxInitialSourceReuse": max(reuse.values()),
        "uniqueCaseFingerprints": len(fingerprints),
        "cases": audit_cases,
    }
    return waves, audit


def write_outputs(output_dir, waves, audit):
    output_dir.mkdir(parents=True, exist_ok=True)
    for wave_no, rows in sorted(waves.items()):
        path = output_dir / f"runtime-wave-{wave_no:02d}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=OUTPUT_FIELDS,
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_suffix(".csv.sha256").write_text(
            f"{digest}  {path.name}\n",
            encoding="utf-8",
        )
        print(f"wrote {path}: rows={len(rows)} sha256={digest}")

    audit_path = output_dir / "runtime-selection-audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {audit_path}")


def main():
    args = parse_args()
    rows = load_rows(args.dataset_manifest)
    waves, audit = build(rows)
    write_outputs(args.output_dir, waves, audit)


if __name__ == "__main__":
    main()
