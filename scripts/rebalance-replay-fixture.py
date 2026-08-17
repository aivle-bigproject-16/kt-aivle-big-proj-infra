#!/usr/bin/env python3
"""Derive a replay fixture with an explicit logical-cell outcome mix.

The source LIVE_RECORD fixture is never modified. PASS conversion removes
recorded defects from the affected inspection. FAIL conversion emits a valid
FAILED callback for one modality while leaving the other modality PASS.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pass-cells", required=True)
    parser.add_argument("--reject-cells", required=True)
    parser.add_argument("--fail-cells", required=True)
    parser.add_argument(
        "--fail-inspection-type",
        choices=("CT", "RGB"),
        default="RGB",
    )
    parser.add_argument("--variant-name", required=True)
    return parser.parse_args()


def parse_cell_list(value):
    cells = [item.strip() for item in value.split(",") if item.strip()]
    if len(cells) != len(set(cells)):
        raise ValueError("cell list contains duplicates")
    return cells


def _validate_fixture(payload):
    if payload.get("schemaVersion") != 1:
        raise ValueError("analysis fixture schemaVersion must be 1")
    if payload.get("mode") != "LIVE_RECORD":
        raise ValueError("analysis fixture mode must be LIVE_RECORD")
    for field in (
        "inspections",
        "inspectionImages",
        "defectResults",
        "replayIndex",
    ):
        if not isinstance(payload.get(field), list):
            raise ValueError(f"analysis fixture field {field} must be a list")


def _patch_raw_response(value, label, confidence):
    was_string = isinstance(value, str)
    if isinstance(value, dict):
        raw = copy.deepcopy(value)
    elif was_string:
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
    else:
        raw = {}

    raw["label"] = label
    raw["confidence"] = confidence
    raw["defects"] = []
    if isinstance(raw.get("quality"), dict):
        raw["quality"]["label"] = "FAIL" if label == "FAIL" else "PASS"
    if label == "FAIL":
        raw["errorCode"] = "REPLAY_SYNTHETIC_AI_FAILURE"
        raw["errorMessage"] = "Synthetic replay failure for QA coverage"
    else:
        raw.pop("errorCode", None)
        raw.pop("errorMessage", None)

    if was_string:
        return json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
    return raw


def _clean_result(row, label):
    result = copy.deepcopy(row)
    confidence = 0.0 if label == "FAIL" else max(
        float(row.get("confidence") or 0.0),
        0.9,
    )
    result["label"] = label
    result["defect_type"] = None
    result["confidence"] = f"{confidence:.4f}"
    result["bbox"] = None
    result["raw_response"] = _patch_raw_response(
        row.get("raw_response"),
        label,
        confidence,
    )
    return result


def _inspection_is_clean_pass(inspection, rows):
    return (
        inspection.get("status") == "COMPLETED"
        and inspection.get("final_label") == "PASS"
        and all(
            row.get("label") == "PASS" and not row.get("defect_type")
            for row in rows
        )
    )


def _replace_inspection_results(rows, label):
    by_image = defaultdict(list)
    for row in rows:
        image_id = row.get("inspection_image_id")
        if image_id is None:
            raise ValueError("replay result is missing inspection_image_id")
        by_image[int(image_id)].append(row)
    return [
        _clean_result(image_rows[0], label)
        for _, image_rows in sorted(by_image.items())
    ]


def _set_pass(inspection, rows):
    if _inspection_is_clean_pass(inspection, rows):
        return rows
    inspection["status"] = "COMPLETED"
    inspection["final_label"] = "PASS"
    inspection["failure_type"] = None
    inspection["failure_reason"] = None
    return _replace_inspection_results(rows, "PASS")


def _set_fail(inspection, rows):
    inspection["status"] = "FAILED"
    inspection["final_label"] = "FAIL"
    inspection["failure_type"] = "AI"
    inspection["failure_reason"] = "REPLAY_SYNTHETIC_AI_FAILURE"
    return _replace_inspection_results(rows, "FAIL")


def cell_outcomes(payload):
    by_cell = defaultdict(list)
    for inspection in payload["inspections"]:
        by_cell[str(inspection["cell_serial_no"])].append(inspection)
    outcomes = Counter()
    for inspections in by_cell.values():
        labels = [
            "FAIL"
            if inspection.get("status") == "FAILED"
            else inspection.get("final_label")
            for inspection in inspections
        ]
        if "FAIL" in labels:
            outcomes["FAIL"] += 1
        elif "REJECT" in labels:
            outcomes["REJECT"] += 1
        else:
            outcomes["PASS"] += 1
    return outcomes


def rebalance_fixture(
    payload,
    pass_cells,
    reject_cells,
    fail_cells,
    fail_inspection_type="RGB",
    variant_name="custom",
    source_sha256=None,
):
    _validate_fixture(payload)
    result = copy.deepcopy(payload)

    target = {}
    for label, cells in (
        ("PASS", pass_cells),
        ("REJECT", reject_cells),
        ("FAIL", fail_cells),
    ):
        for cell in cells:
            if cell in target:
                raise ValueError(f"cell appears in multiple outcomes: {cell}")
            target[cell] = label

    inspections_by_cell = defaultdict(list)
    for inspection in result["inspections"]:
        inspections_by_cell[str(inspection["cell_serial_no"])].append(inspection)
    source_cells = set(inspections_by_cell)
    if set(target) != source_cells:
        missing = sorted(source_cells - set(target))
        unknown = sorted(set(target) - source_cells)
        raise ValueError(f"cell partition mismatch: missing={missing} unknown={unknown}")

    rows_by_inspection = defaultdict(list)
    for row in result["defectResults"]:
        rows_by_inspection[int(row["inspection_id"])].append(row)

    replacement_rows = {}
    for cell, inspections in inspections_by_cell.items():
        desired = target[cell]
        current_rejects = [
            inspection
            for inspection in inspections
            if inspection.get("status") == "COMPLETED"
            and inspection.get("final_label") == "REJECT"
        ]
        if desired == "REJECT" and not current_rejects:
            raise ValueError(
                f"cannot synthesize a credible REJECT without a recorded defect: {cell}"
            )
        fail_matches = [
            inspection
            for inspection in inspections
            if inspection.get("inspection_type") == fail_inspection_type
        ]
        if desired == "FAIL" and len(fail_matches) != 1:
            raise ValueError(
                f"cell {cell} must have exactly one {fail_inspection_type} inspection"
            )

        for inspection in inspections:
            inspection_id = int(inspection["id"])
            rows = rows_by_inspection.get(inspection_id, [])
            if not rows:
                raise ValueError(f"inspection {inspection_id} has no result rows")
            if desired == "REJECT" and inspection in current_rejects:
                replacement_rows[inspection_id] = rows
            elif desired == "FAIL" and inspection is fail_matches[0]:
                replacement_rows[inspection_id] = _set_fail(inspection, rows)
            else:
                replacement_rows[inspection_id] = _set_pass(inspection, rows)

    flattened = []
    emitted = set()
    for row in result["defectResults"]:
        inspection_id = int(row["inspection_id"])
        if inspection_id in emitted:
            continue
        flattened.extend(replacement_rows[inspection_id])
        emitted.add(inspection_id)
    result["defectResults"] = flattened

    expected = Counter(
        {
            "PASS": len(pass_cells),
            "REJECT": len(reject_cells),
            "FAIL": len(fail_cells),
        }
    )
    actual = cell_outcomes(result)
    if actual != expected:
        raise ValueError(f"outcome verification failed: expected={expected} actual={actual}")

    result["replayVariant"] = {
        "name": variant_name,
        "sourceSha256": source_sha256,
        "cellFinalLabels": dict(sorted(actual.items())),
        "passCells": list(pass_cells),
        "rejectCells": list(reject_cells),
        "failCells": list(fail_cells),
        "failInspectionType": fail_inspection_type,
        "synthetic": True,
    }
    return result


def main():
    args = parse_args()
    source_bytes = args.input.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    payload = json.loads(source_bytes)
    result = rebalance_fixture(
        payload,
        parse_cell_list(args.pass_cells),
        parse_cell_list(args.reject_cells),
        parse_cell_list(args.fail_cells),
        fail_inspection_type=args.fail_inspection_type,
        variant_name=args.variant_name,
        source_sha256=source_sha256,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{digest}  {args.output.name}\n",
        encoding="utf-8",
    )
    counts = cell_outcomes(result)
    print(
        f"wrote {args.output}: PASS={counts['PASS']} "
        f"REJECT={counts['REJECT']} FAIL={counts['FAIL']} sha256={digest}"
    )


if __name__ == "__main__":
    main()
