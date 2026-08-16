#!/usr/bin/env python3
"""Build Wave 1 demo v2 by calibrating only normal-cell CT images.

RGB rows and defective-cell CT rows are retained from the v1.8 demo v1
manifest.  Normal-cell CT rows are selected from immutable v1.7 objects whose
recorded product decisions were consistently PASS.  This is a demo fixture,
not an unbiased evaluation dataset.
"""

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


SEED = 20260817
DATASET_VERSION = "v1.8-demo-model-calibrated-v2"
FIXTURES = (
    "wave-01-run-6.json",
    "wave-02-run-7.json",
    "wave-03-run-8.json",
    "wave-04-run-9.json",
    "wave-05-run-10.json",
)
AXIS_COUNTS = {"x": 5, "y": 17, "z": 18}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-manifest-dir", type=Path, required=True)
    parser.add_argument("--input-wave", type=Path, required=True)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return list(reader.fieldnames or ()), list(reader)


def load_sources(manifest_dir):
    source_by_key = {}
    for path in sorted(manifest_dir.glob("runtime-wave-*.csv")):
        _, rows = read_csv(path)
        for row in rows:
            if row["image_type"] != "CT" or row["capture_set"] != "INITIAL":
                continue
            key = row["object_key"]
            existing = source_by_key.get(key)
            if existing and (
                existing["product_status"], existing["axis"]
            ) != (row["product_status"], row["axis"]):
                raise ValueError(f"conflicting source metadata for {key}")
            source_by_key.setdefault(key, row)
    if not source_by_key:
        raise ValueError("no CT INITIAL source rows found")
    return source_by_key


def load_observations(replay_dir):
    occurrences = {}
    for name in FIXTURES:
        payload = json.loads((replay_dir / name).read_text(encoding="utf-8"))
        images = {row["id"]: row for row in payload["inspectionImages"]}
        for result in payload["defectResults"]:
            if result["image_type"] != "CT" or result.get("attempt_no") != 1:
                continue
            raw = result.get("raw_response") or {}
            if result["inspection_image_id"] is not None:
                items = [(
                    result["inspection_image_id"],
                    result["label"],
                    result.get("confidence"),
                    raw,
                )]
            else:
                items = [(
                    item["imageId"],
                    item["label"],
                    item.get("confidence"),
                    item.get("rawResponse") or {},
                ) for item in raw.get("imageResults") or ()]
            for image_id, label, confidence, item_raw in items:
                image = images.get(image_id)
                if image is None:
                    raise ValueError(f"missing inspection image {image_id}")
                key = image.get("source_object_key") or image["object_key"]
                quality = item_raw.get("quality") or {}
                occurrences[(result["inspection_id"], image_id)] = {
                    "object_key": key,
                    "quality_label": quality.get("label"),
                    "quality_confidence": quality.get("confidence"),
                    "product_label": label,
                    "product_confidence": confidence,
                }
    observations = defaultdict(list)
    for observation in occurrences.values():
        observations[observation["object_key"]].append(observation)
    return observations, len(occurrences)


def minimum(values):
    numbers = [float(value) for value in values if value is not None]
    return min(numbers) if numbers else ""


def build_index(source_by_key, observations):
    rows = []
    pools = defaultdict(list)
    for key, source in sorted(source_by_key.items()):
        seen = observations.get(key, [])
        product_labels = {row["product_label"] for row in seen}
        if (
            source["product_status"] == "normal"
            and source["quality_label"] == "PASS"
            and seen
            and product_labels == {"PASS"}
        ):
            candidate_class = "NORMAL_PRODUCT_PASS"
            pools[source["axis"]].append(source)
        elif not seen:
            candidate_class = "UNOBSERVED"
        else:
            candidate_class = "OTHER"
        rows.append({
            "object_key": key,
            "source_sample_id": source["source_sample_id"],
            "source_image_sha256": source["source_image_sha256"],
            "original_battery_id": source["original_battery_id"],
            "axis": source["axis"],
            "source_quality_label": source["quality_label"],
            "source_product_status": source["product_status"],
            "observation_count": len(seen),
            "observed_quality_labels": ";".join(sorted({
                row["quality_label"] or "" for row in seen
            })),
            "observed_product_labels": ";".join(sorted({
                row["product_label"] or "" for row in seen
            })),
            "minimum_quality_confidence": minimum(
                row["quality_confidence"] for row in seen
            ),
            "minimum_product_confidence": minimum(
                row["product_confidence"] for row in seen
            ),
            "candidate_class": candidate_class,
        })
    return rows, pools


class AxisPool:
    def __init__(self, rows, seed):
        self.rows = list(rows)
        random.Random(seed).shuffle(self.rows)
        self.cursor = 0
        self.usage = Counter()

    def take(self, count):
        if len(self.rows) < count:
            raise ValueError(f"axis pool has only {len(self.rows)} rows")
        chosen = []
        used_in_case = set()
        for allow_reuse in (False, True):
            visited = 0
            while len(chosen) < count and visited < len(self.rows) * 2:
                row = self.rows[self.cursor % len(self.rows)]
                self.cursor += 1
                visited += 1
                key = row["object_key"]
                if key in used_in_case:
                    continue
                if not allow_reuse and self.usage[key]:
                    continue
                chosen.append(row)
                used_in_case.add(key)
                self.usage[key] += 1
            if len(chosen) == count:
                return chosen
        raise RuntimeError(f"could not select {count} unique rows for one case")


def object_multiset(rows, predicate):
    return Counter(row["object_key"] for row in rows if predicate(row))


def build_wave(input_rows, pools):
    axis_pools = {
        axis: AxisPool(pools[axis], SEED + offset)
        for offset, axis in enumerate(sorted(AXIS_COUNTS))
    }
    cases = defaultdict(list)
    for row in input_rows:
        cases[row["logical_case_id"]].append(row)

    output = []
    case_audit = []
    for logical_case_id in sorted(cases):
        case_rows = cases[logical_case_id]
        initial_ct = [
            row for row in case_rows
            if row["image_type"] == "CT" and row["capture_set"] == "INITIAL"
        ]
        statuses = {row["product_status"] for row in initial_ct}
        if len(initial_ct) != 40 or len(statuses) != 1:
            raise ValueError(f"invalid CT case {logical_case_id}")
        status = next(iter(statuses))

        if status == "defective":
            selected_ct = [dict(row) for row in case_rows if row["image_type"] == "CT"]
            policy = "preserved-v1.8-demo-v1-defective-ct"
        else:
            template = initial_ct[0]
            selected_ct = []
            for axis, count in AXIS_COUNTS.items():
                for source in axis_pools[axis].take(count):
                    row = dict(source)
                    row.update({
                        "wave_no": "1",
                        "logical_case_id": logical_case_id,
                        "cell_serial_no": template["cell_serial_no"],
                        "capture_set": "INITIAL",
                        "quality_label": "PASS",
                        "product_status": "normal",
                        "dataset_version": DATASET_VERSION,
                        "selection_policy": "recorded-ct-product-pass",
                    })
                    selected_ct.append(row)
            policy = "recorded-ct-product-pass"

        rgb_rows = [dict(row) for row in case_rows if row["image_type"] == "RGB"]
        for row in selected_ct + rgb_rows:
            row["dataset_version"] = DATASET_VERSION
            output.append(row)
        case_audit.append({
            "logicalCaseId": logical_case_id,
            "cellSerialNo": initial_ct[0]["cell_serial_no"],
            "ctProductStatus": status,
            "ctSelectionPolicy": policy,
            "ctRows": len(selected_ct),
            "rgbRows": len(rgb_rows),
        })

    if len(output) != 1600:
        raise AssertionError(f"expected 1600 rows, found {len(output)}")
    if object_multiset(input_rows, lambda row: row["image_type"] == "RGB") != object_multiset(
        output, lambda row: row["image_type"] == "RGB"
    ):
        raise AssertionError("RGB object multiset changed")
    defect = lambda row: (
        row["image_type"] == "CT" and row["product_status"] == "defective"
    )
    if object_multiset(input_rows, defect) != object_multiset(output, defect):
        raise AssertionError("defective CT object multiset changed")

    for logical_case_id, rows in cases_from(output).items():
        ct_rows = [row for row in rows if row["image_type"] == "CT"]
        if len(ct_rows) != 40 or any(row["capture_set"] != "INITIAL" for row in ct_rows):
            raise AssertionError(f"CT capture plan invalid for {logical_case_id}")
        if Counter(row["axis"] for row in ct_rows) != Counter(AXIS_COUNTS):
            raise AssertionError(f"CT axis plan invalid for {logical_case_id}")
    return output, case_audit, axis_pools


def cases_from(rows):
    cases = defaultdict(list)
    for row in rows:
        cases[row["logical_case_id"]].append(row)
    return cases


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    source_by_key = load_sources(args.source_manifest_dir)
    input_fields, input_rows = read_csv(args.input_wave)
    observations, occurrence_count = load_observations(args.replay_dir)
    index_rows, pools = build_index(source_by_key, observations)
    output_rows, cases, axis_pools = build_wave(input_rows, pools)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.output_dir / "ct-model-screening-index.csv"
    write_csv(index_path, list(index_rows[0]), index_rows)

    wave_path = args.output_dir / "runtime-wave-01.csv"
    output_fields = list(input_fields)
    for field in ("dataset_version", "selection_policy"):
        if field not in output_fields:
            output_fields.append(field)
    write_csv(wave_path, output_fields, output_rows)
    wave_sha = hashlib.sha256(wave_path.read_bytes()).hexdigest()
    wave_path.with_suffix(".csv.sha256").write_text(
        f"{wave_sha}  {wave_path.name}\n", encoding="utf-8"
    )

    candidate_counts = {axis: len(pools[axis]) for axis in sorted(AXIS_COUNTS)}
    audit = {
        "schemaVersion": 1,
        "datasetVersion": DATASET_VERSION,
        "sourceDataset": "v1.7 immutable S3 objects",
        "inputManifest": str(args.input_wave),
        "selectionEvidence": list(FIXTURES),
        "observedInitialCtOccurrences": occurrence_count,
        "uniqueObservedCtKeys": len(observations),
        "candidateCountsByAxis": candidate_counts,
        "candidateMaxReuseByAxis": {
            axis: max(pool.usage.values(), default=0)
            for axis, pool in axis_pools.items()
        },
        "waveOneRows": len(output_rows),
        "waveOneSha256": wave_sha,
        "cases": cases,
        "invariants": {
            "rgbObjectsPreserved": True,
            "defectiveCtObjectsPreserved": True,
            "normalCtRecaptures": 0,
            "ctAxisCountsPerCell": AXIS_COUNTS,
        },
        "limitation": (
            "Demo-only model-calibrated selection; do not report as an unbiased "
            "accuracy evaluation. CT quality is not a product recapture gate."
        ),
    }
    audit_path = args.output_dir / "v18-ct-selection-audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
