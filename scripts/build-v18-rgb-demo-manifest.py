#!/usr/bin/env python3
"""Build a demo-only v1.8 Wave 1 manifest from recorded RGB outcomes.

The source images remain immutable under the v1.7 S3 prefix.  V1.8 is a
manifest-layer version that records model-calibrated selection separately,
without duplicating the 171 GB source dataset.
"""

import argparse
import csv
import hashlib
import json
import random
from collections import Counter, defaultdict
from pathlib import Path


SEED = 20260817
DATASET_VERSION = "v1.8-demo-model-calibrated"
FIXTURES = (
    "wave-01-run-6.json",
    "wave-02-run-7.json",
    "wave-03-run-8.json",
    "wave-04-run-9.json",
    "wave-05-run-10.json",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-dir", type=Path, required=True)
    parser.add_argument("--replay-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser.parse_args()


def load_manifests(manifest_dir):
    rows_by_wave = {}
    source_by_key = {}
    fieldnames = None
    for path in sorted(manifest_dir.glob("runtime-wave-*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if fieldnames is None:
                fieldnames = list(reader.fieldnames or ())
            rows = list(reader)
        wave_no = int(rows[0]["wave_no"])
        rows_by_wave[wave_no] = rows
        for row in rows:
            if row["image_type"] != "RGB" or row["capture_set"] != "INITIAL":
                continue
            key = row["object_key"]
            existing = source_by_key.get(key)
            if existing and existing["product_status"] != row["product_status"]:
                raise ValueError(f"conflicting product status for {key}")
            source_by_key.setdefault(key, row)
    if set(rows_by_wave) != {1, 2, 3, 4, 5}:
        raise ValueError(f"expected five waves, found {sorted(rows_by_wave)}")
    return rows_by_wave, source_by_key, fieldnames


def load_observations(replay_dir):
    occurrences = {}
    for name in FIXTURES:
        path = replay_dir / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        images = {row["id"]: row for row in payload["inspectionImages"]}
        for result in payload["defectResults"]:
            if result["image_type"] != "RGB" or result.get("attempt_no") != 1:
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


def candidate_class(source, observations):
    if not observations:
        return "UNOBSERVED"
    quality_pass = all(
        row["quality_label"] == "PASS" for row in observations
    )
    product_labels = {row["product_label"] for row in observations}
    if (
        source["product_status"] == "normal"
        and quality_pass
        and product_labels == {"PASS"}
    ):
        return "NORMAL_DUAL_PASS"
    if (
        source["product_status"] == "defective"
        and quality_pass
        and product_labels == {"REJECT"}
    ):
        return "DEFECT_DUAL_REJECT"
    return "OTHER"


def minimum(values):
    numbers = [float(value) for value in values if value is not None]
    return min(numbers) if numbers else ""


def build_index(source_by_key, observations):
    rows = []
    pools = defaultdict(list)
    for key, source in sorted(source_by_key.items()):
        seen = observations.get(key, [])
        classification = candidate_class(source, seen)
        row = {
            "object_key": key,
            "source_sample_id": source["source_sample_id"],
            "source_image_sha256": source["source_image_sha256"],
            "original_battery_id": source["original_battery_id"],
            "source_product_status": source["product_status"],
            "observation_count": len(seen),
            "observed_quality_labels": ";".join(sorted({
                item["quality_label"] or "" for item in seen
            })),
            "observed_product_labels": ";".join(sorted({
                item["product_label"] or "" for item in seen
            })),
            "minimum_quality_confidence": minimum(
                item["quality_confidence"] for item in seen
            ),
            "minimum_product_confidence": minimum(
                item["product_confidence"] for item in seen
            ),
            "candidate_class": classification,
        }
        rows.append(row)
        if classification != "OTHER" and classification != "UNOBSERVED":
            pools[classification].append(source)
    return rows, pools


class Pool:
    def __init__(self, rows, seed):
        self.rows = list(rows)
        random.Random(seed).shuffle(self.rows)
        self.cursor = 0
        self.usage = Counter()

    def take(self, count):
        if len(self.rows) < count:
            raise ValueError(f"candidate pool has only {len(self.rows)} rows")
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


def build_wave_one(source_rows, pools):
    normal_pool = Pool(pools["NORMAL_DUAL_PASS"], SEED)
    defect_pool = Pool(pools["DEFECT_DUAL_REJECT"], SEED + 1)
    cases = defaultdict(list)
    for row in source_rows:
        cases[row["logical_case_id"]].append(row)

    output = []
    case_audit = []
    for logical_case_id in sorted(cases):
        case_rows = cases[logical_case_id]
        rgb_rows = [
            row for row in case_rows
            if row["image_type"] == "RGB"
            and row["capture_set"] == "INITIAL"
        ]
        rgb_recaptures = [
            row for row in case_rows
            if row["image_type"] == "RGB"
            and row["capture_set"] == "RECAPTURE"
        ]
        if len(rgb_rows) != 40 or rgb_recaptures:
            raise ValueError(
                f"Wave 1 {logical_case_id} must have 40 RGB INITIAL only"
            )
        statuses = {row["product_status"] for row in rgb_rows}
        if len(statuses) != 1:
            raise ValueError(f"mixed RGB status in {logical_case_id}")
        status = next(iter(statuses))
        pool = normal_pool if status == "normal" else defect_pool
        selected = pool.take(40)

        output.extend(row for row in case_rows if row["image_type"] == "CT")
        template = rgb_rows[0]
        for source in selected:
            row = dict(source)
            row.update({
                "wave_no": "1",
                "logical_case_id": logical_case_id,
                "cell_serial_no": template["cell_serial_no"],
                "capture_set": "INITIAL",
                "quality_label": "PASS",
                "product_status": status,
                "dataset_version": DATASET_VERSION,
                "selection_policy": (
                    "recorded-quality-pass+product-pass"
                    if status == "normal"
                    else "recorded-quality-pass+product-reject"
                ),
            })
            output.append(row)
        case_audit.append({
            "logicalCaseId": logical_case_id,
            "cellSerialNo": template["cell_serial_no"],
            "rgbProductStatus": status,
            "candidateClass": (
                "NORMAL_DUAL_PASS"
                if status == "normal"
                else "DEFECT_DUAL_REJECT"
            ),
            "selectedImages": 40,
        })

    if len(output) != len(source_rows):
        raise AssertionError(
            f"row count changed: {len(source_rows)} -> {len(output)}"
        )
    return output, case_audit, normal_pool, defect_pool


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=fieldnames,
            lineterminator="\n",
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def main():
    args = parse_args()
    waves, source_by_key, original_fields = load_manifests(args.manifest_dir)
    observations, occurrence_count = load_observations(args.replay_dir)
    index_rows, pools = build_index(source_by_key, observations)
    wave_rows, cases, normal_pool, defect_pool = build_wave_one(
        waves[1], pools
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    index_path = args.output_dir / "rgb-model-screening-index.csv"
    index_fields = list(index_rows[0])
    write_csv(index_path, index_fields, index_rows)

    wave_path = args.output_dir / "runtime-wave-01.csv"
    wave_fields = list(original_fields)
    for field in ("dataset_version", "selection_policy"):
        if field not in wave_fields:
            wave_fields.append(field)
    write_csv(wave_path, wave_fields, wave_rows)
    wave_sha = hashlib.sha256(wave_path.read_bytes()).hexdigest()
    wave_path.with_suffix(".csv.sha256").write_text(
        f"{wave_sha}  {wave_path.name}\n",
        encoding="utf-8",
    )

    class_counts = Counter(row["candidate_class"] for row in index_rows)
    audit = {
        "schemaVersion": 1,
        "datasetVersion": DATASET_VERSION,
        "sourceDataset": "v1.7 immutable S3 objects",
        "selectionEvidence": list(FIXTURES),
        "observedInitialRgbOccurrences": occurrence_count,
        "uniqueObservedRgbKeys": len(observations),
        "candidateCounts": dict(sorted(class_counts.items())),
        "waveOneRows": len(wave_rows),
        "waveOneSha256": wave_sha,
        "normalCandidateMaxReuse": max(normal_pool.usage.values()),
        "defectCandidateMaxReuse": max(defect_pool.usage.values()),
        "cases": cases,
        "limitation": (
            "Demo-only model-calibrated selection; do not report as an "
            "unbiased accuracy evaluation."
        ),
    }
    audit_path = args.output_dir / "v18-selection-audit.json"
    audit_path.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
