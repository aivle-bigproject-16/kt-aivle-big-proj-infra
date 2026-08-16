import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / (
    "build-v17-runtime-manifests.py"
)
SPEC = importlib.util.spec_from_file_location("runtime_manifests", SCRIPT)
runtime_manifests = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime_manifests)


def source_row(sample_id, axis, sequence, has_porosity="0"):
    return {
        "sample_id": sample_id,
        "modality": "CT",
        "product_status": "defective",
        "capture_quality": "PASS",
        "axis": axis,
        "output_sequence_order": str(sequence),
        "has_porosity": has_porosity,
    }


def test_ct_defective_case_contains_three_annotated_porosity_images():
    rows = []
    for axis, count in runtime_manifests.CT_AXIS_PLAN.items():
        for index in range(count):
            rows.append(source_row(f"{axis}-normal-{index}", axis, index))
    rows += [
        source_row(f"y-porosity-{index}", "y", 100 + index, "1")
        for index in range(3)
    ]
    selectors = runtime_manifests.build_selectors(rows, set())

    selected = runtime_manifests.choose_initial(
        selectors,
        case_no=1,
        modality="CT",
        product_status="defective",
        capture_fail=False,
    )

    assert len(selected) == 40
    assert sum(row["has_porosity"] == "1" for row in selected) == 3
    assert {
        axis: sum(row["axis"] == axis for row in selected)
        for axis in runtime_manifests.CT_AXIS_PLAN
    } == runtime_manifests.CT_AXIS_PLAN
