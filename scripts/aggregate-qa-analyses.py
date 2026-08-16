#!/usr/bin/env python3
"""Aggregate per-wave v1.7 QA analysis artifacts."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


CONFUSION_KEYS = (
    "trueNegative",
    "falsePositive",
    "falseNegative",
    "truePositive",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("analyses", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def add_confusion(target, source):
    for modality, counts in source.items():
        for key in CONFUSION_KEYS:
            target[modality][key] += counts[key]


def main():
    args = parse_args()
    analyses = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in args.analyses
    ]
    analyses.sort(key=lambda item: item["waveNo"])

    terminal = Counter()
    inspection_outcomes = Counter()
    capture_confusion = defaultdict(Counter)
    defect_confusion = defaultdict(Counter)
    defect_any_image_confusion = defaultdict(Counter)
    defect_not_evaluated = Counter()
    cells_by_case = defaultdict(list)
    latency_waves = defaultdict(list)

    for analysis in analyses:
        wave_no = analysis["waveNo"]
        terminal.update(analysis["terminalInspectionCounts"])
        for outcome in analysis["inspectionOutcomes"]:
            key = (
                outcome["modality"],
                outcome["status"],
                outcome["finalLabel"],
                outcome["failureType"],
            )
            inspection_outcomes[key] += outcome["count"]
        add_confusion(
            capture_confusion,
            analysis["captureQualityCellConfusion"],
        )
        add_confusion(defect_confusion, analysis["defectCellConfusion"])
        add_confusion(
            defect_any_image_confusion,
            analysis.get("defectAnyImageConfusion", {}),
        )
        defect_not_evaluated.update(
            analysis.get("defectCellNotEvaluated", {})
        )
        for modality, metrics in analysis["imageLatencyMs"].items():
            latency_waves[modality].append(metrics)
        for cell in analysis["cells"]:
            cells_by_case[(wave_no, cell["cellSerialNo"])].append(cell)

    cell_outcomes = Counter()
    expected_actual = Counter()
    cases = []
    priority = {"PASS": 0, "REJECT": 1, "FAIL": 2}
    for (wave_no, serial_no), inspections in sorted(cells_by_case.items()):
        labels = [
            "FAIL" if item["status"] == "FAILED" else item["finalLabel"]
            for item in inspections
        ]
        actual = max(labels, key=priority.get)
        expected = (
            "REJECT" if any(item["expectedDefect"] for item in inspections)
            else "PASS"
        )
        cell_outcomes[actual] += 1
        expected_actual[(expected, actual)] += 1
        cases.append({
            "waveNo": wave_no,
            "cellSerialNo": serial_no,
            "expectedFinalLabel": expected,
            "actualFinalLabel": actual,
            "inspectionLabels": {
                item["modality"]: (
                    "FAIL" if item["status"] == "FAILED"
                    else item["finalLabel"]
                )
                for item in inspections
            },
        })

    summary = {
        "schemaVersion": 2,
        "waveCount": len(analyses),
        "logicalCaseCount": len(cases),
        "runIds": [analysis["runIds"][0] for analysis in analyses],
        "terminalInspectionCounts": dict(sorted(terminal.items())),
        "inspectionOutcomes": [
            {
                "modality": key[0],
                "status": key[1],
                "finalLabel": key[2],
                "failureType": key[3],
                "count": count,
            }
            for key, count in sorted(
                inspection_outcomes.items(),
                key=lambda item: tuple("" if x is None else x for x in item[0]),
            )
        ],
        "cellFinalLabels": dict(sorted(cell_outcomes.items())),
        "cellExpectedActualMatrix": [
            {"expected": key[0], "actual": key[1], "count": count}
            for key, count in sorted(expected_actual.items())
        ],
        "captureQualityCellConfusion": {
            modality: dict(counter)
            for modality, counter in sorted(capture_confusion.items())
        },
        "defectCellConfusion": {
            modality: dict(counter)
            for modality, counter in sorted(defect_confusion.items())
        },
        "defectCellNotEvaluated": dict(sorted(defect_not_evaluated.items())),
        "defectAnyImageConfusion": {
            modality: dict(counter)
            for modality, counter in sorted(
                defect_any_image_confusion.items()
            )
        },
        "imageLatencyMsAcrossWaves": {
            modality: {
                "imageCount": sum(item["count"] for item in metrics),
                "p50Range": [
                    min(item["p50"] for item in metrics),
                    max(item["p50"] for item in metrics),
                ],
                "p95Range": [
                    min(item["p95"] for item in metrics),
                    max(item["p95"] for item in metrics),
                ],
                "max": max(item["max"] for item in metrics),
            }
            for modality, metrics in sorted(latency_waves.items())
        },
        "cases": cases,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{digest}  {args.output.name}\n",
        encoding="utf-8",
    )
    print(content, end="")


if __name__ == "__main__":
    main()
