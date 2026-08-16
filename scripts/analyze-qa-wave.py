#!/usr/bin/env python3
"""Compare a v1.7 runtime manifest with one LIVE replay fixture."""

import argparse
import csv
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * quantile)]


def callback_image_results(defect_results, attempt_no):
    for result in defect_results:
        if result["attempt_no"] != attempt_no:
            continue
        raw = result.get("raw_response")
        if isinstance(raw, dict) and isinstance(raw.get("imageResults"), list):
            return raw["imageResults"]
    return []


def completed_image_results(defect_results, attempt_no):
    by_image = {}
    for result in defect_results:
        if result["attempt_no"] != attempt_no:
            continue
        image_id = result.get("inspection_image_id")
        raw = result.get("raw_response")
        if image_id is None or not isinstance(raw, dict):
            continue
        by_image.setdefault(image_id, {
            "imageId": image_id,
            "label": raw.get("label", result.get("label")),
            "latencyMs": result.get("latency_ms"),
            "rawResponse": raw,
            "errorCode": None,
        })
    return list(by_image.values())


def main():
    args = parse_args()
    with args.manifest.open(encoding="utf-8", newline="") as source:
        manifest_rows = list(csv.DictReader(source))
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))

    ground_truth = {}
    expected_by_cell = {}
    for row in manifest_rows:
        key = row["object_key"]
        ground_truth[key] = row
        if row["capture_set"] == "INITIAL":
            expected_by_cell[
                (row["cell_serial_no"], row["image_type"])
            ] = row["product_status"]

    images_by_id = {
        image["id"]: image for image in fixture["inspectionImages"]
    }
    images_by_inspection = defaultdict(list)
    for image in fixture["inspectionImages"]:
        images_by_inspection[image["inspection_id"]].append(image)
    results_by_inspection = defaultdict(list)
    for result in fixture["defectResults"]:
        results_by_inspection[result["inspection_id"]].append(result)

    outcome_counts = Counter()
    capture_confusion = defaultdict(Counter)
    defect_confusion = defaultdict(Counter)
    defect_any_image_confusion = defaultdict(Counter)
    defect_not_evaluated = Counter()
    latency_by_modality = defaultdict(list)
    cells = []

    for inspection in fixture["inspections"]:
        inspection_id = inspection["id"]
        modality = inspection["inspection_type"]
        serial_no = inspection["cell_serial_no"]
        outcome_counts[
            (
                modality,
                inspection["status"],
                inspection.get("final_label"),
                inspection.get("failure_type"),
            )
        ] += 1

        initial_images = [
            image for image in images_by_inspection[inspection_id]
            if image["attempt_no"] == 1
        ]
        defect_results = results_by_inspection[inspection_id]
        predictions = callback_image_results(defect_results, 1)
        if not predictions:
            predictions = completed_image_results(defect_results, 1)

        quality_pass = 0
        reject_count = 0
        errors = 0
        for prediction in predictions:
            raw = prediction.get("rawResponse") or {}
            quality = raw.get("quality") or {}
            quality_label = quality.get("label")
            if quality_label is None:
                quality_label = (
                    "FAIL" if prediction.get("label") == "FAIL" else "PASS"
                )
            quality_pass += quality_label == "PASS"
            reject_count += prediction.get("label") == "REJECT"
            errors += prediction.get("errorCode") is not None
            latency = prediction.get("latencyMs")
            if latency is not None:
                latency_by_modality[modality].append(latency)

        ground_truth_pass = sum(
            ground_truth[image["source_object_key"]]["quality_label"] == "PASS"
            for image in initial_images
        )
        expected_capture_fail = (
            len(initial_images) == 0
            or ground_truth_pass / len(initial_images) < 0.8
        )
        observed_capture_fail = (
            len(predictions) == 0
            or quality_pass / len(predictions) < 0.8
        )
        capture_confusion[modality][
            (expected_capture_fail, observed_capture_fail)
        ] += 1

        expected_defect = (
            expected_by_cell[(serial_no, modality)] == "defective"
        )
        any_reject_image = reject_count > 0
        defect_any_image_confusion[modality][
            (expected_defect, any_reject_image)
        ] += 1
        final_label = inspection.get("final_label")
        if inspection["status"] == "COMPLETED" and final_label in {
            "PASS",
            "REJECT",
        }:
            predicted_defect = final_label == "REJECT"
            defect_confusion[modality][
                (expected_defect, predicted_defect)
            ] += 1
        else:
            predicted_defect = None
            defect_not_evaluated[modality] += 1

        cells.append({
            "cellSerialNo": serial_no,
            "modality": modality,
            "status": inspection["status"],
            "finalLabel": inspection.get("final_label"),
            "failureType": inspection.get("failure_type"),
            "captureRetryCount": inspection["capture_retry_count"],
            "initialImageCount": len(initial_images),
            "groundTruthQualityPass": ground_truth_pass,
            "observedQualityPass": quality_pass,
            "observedImageCount": len(predictions),
            "expectedCaptureFail": expected_capture_fail,
            "observedCaptureFail": observed_capture_fail,
            "expectedDefect": expected_defect,
            "predictedDefect": predicted_defect,
            "anyRejectImage": any_reject_image,
            "rejectImageCount": reject_count,
            "errorImageCount": errors,
        })

    def named_confusion(counter):
        return {
            "trueNegative": counter[(False, False)],
            "falsePositive": counter[(False, True)],
            "falseNegative": counter[(True, False)],
            "truePositive": counter[(True, True)],
        }

    summary = {
        "schemaVersion": 2,
        "waveNo": fixture["waveNo"],
        "runIds": fixture["runIds"],
        "manifestSha256": fixture["manifestSha256"],
        "terminalInspectionCounts": fixture["terminalInspectionCounts"],
        "inspectionOutcomes": [
            {
                "modality": key[0],
                "status": key[1],
                "finalLabel": key[2],
                "failureType": key[3],
                "count": count,
            }
            for key, count in sorted(
                outcome_counts.items(),
                key=lambda item: tuple("" if x is None else x for x in item[0]),
            )
        ],
        "captureQualityCellConfusion": {
            modality: named_confusion(counter)
            for modality, counter in sorted(capture_confusion.items())
        },
        "defectCellConfusion": {
            modality: named_confusion(counter)
            for modality, counter in sorted(defect_confusion.items())
        },
        "defectCellNotEvaluated": dict(sorted(defect_not_evaluated.items())),
        "defectAnyImageConfusion": {
            modality: named_confusion(counter)
            for modality, counter in sorted(
                defect_any_image_confusion.items()
            )
        },
        "imageLatencyMs": {
            modality: {
                "count": len(values),
                "p50": percentile(values, 0.50),
                "p95": percentile(values, 0.95),
                "max": max(values) if values else None,
            }
            for modality, values in sorted(latency_by_modality.items())
        },
        "cells": cells,
    }
    output = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
        args.output.with_suffix(args.output.suffix + ".sha256").write_text(
            f"{digest}  {args.output.name}\n",
            encoding="utf-8",
        )
    print(output)


if __name__ == "__main__":
    main()
