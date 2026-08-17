import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).parents[1] / "scripts" / "rebalance-replay-fixture.py"
SPEC = importlib.util.spec_from_file_location("rebalance_replay_fixture", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture():
    inspections = []
    images = []
    results = []
    replay_index = []
    inspection_id = 1
    image_id = 100
    result_id = 1000
    for cell, reject_type in (
        ("SIM-0001", "RGB"),
        ("SIM-0002", "CT"),
        ("SIM-0003", None),
    ):
        for image_type in ("CT", "RGB"):
            rejected = image_type == reject_type
            inspections.append(
                {
                    "id": inspection_id,
                    "cell_serial_no": cell,
                    "inspection_type": image_type,
                    "status": "COMPLETED",
                    "final_label": "REJECT" if rejected else "PASS",
                    "failure_type": None,
                    "failure_reason": None,
                }
            )
            images.append(
                {
                    "id": image_id,
                    "inspection_id": inspection_id,
                    "image_type": image_type,
                    "bucket_name": "fixture",
                    "object_key": f"{cell}/{image_type}.png",
                    "attempt_no": 1,
                }
            )
            results.append(
                {
                    "id": result_id,
                    "inspection_id": inspection_id,
                    "inspection_image_id": image_id,
                    "image_type": image_type,
                    "label": "REJECT" if rejected else "PASS",
                    "defect_type": "CRACK" if rejected else None,
                    "confidence": "0.5000",
                    "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}
                    if rejected
                    else None,
                    "raw_response": {
                        "label": "REJECT" if rejected else "PASS",
                        "confidence": 0.5,
                        "defects": [{"defectType": "CRACK"}] if rejected else [],
                    },
                }
            )
            replay_index.append(
                {
                    "inspectionId": inspection_id,
                    "attemptNo": 1,
                    "imageCount": 1,
                    "requestFingerprint": f"fingerprint-{inspection_id}",
                }
            )
            inspection_id += 1
            image_id += 1
            result_id += 1
    return {
        "schemaVersion": 1,
        "mode": "LIVE_RECORD",
        "inspections": inspections,
        "inspectionImages": images,
        "defectResults": results,
        "replayIndex": replay_index,
    }


class RebalanceReplayFixtureTest(unittest.TestCase):
    def test_rebalances_cell_outcomes_and_keeps_contract_consistent(self):
        result = MODULE.rebalance_fixture(
            fixture(),
            pass_cells=["SIM-0001"],
            reject_cells=["SIM-0002"],
            fail_cells=["SIM-0003"],
            fail_inspection_type="RGB",
            variant_name="test-1-1-1",
            source_sha256="a" * 64,
        )

        self.assertEqual(
            MODULE.cell_outcomes(result),
            {"PASS": 1, "REJECT": 1, "FAIL": 1},
        )
        converted_pass = [
            row
            for row in result["inspections"]
            if row["cell_serial_no"] == "SIM-0001"
        ]
        self.assertEqual(
            {row["final_label"] for row in converted_pass},
            {"PASS"},
        )
        self.assertTrue(
            all(row["status"] == "COMPLETED" for row in converted_pass)
        )

        failed = next(
            row
            for row in result["inspections"]
            if row["cell_serial_no"] == "SIM-0003"
            and row["inspection_type"] == "RGB"
        )
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(failed["final_label"], "FAIL")
        self.assertEqual(failed["failure_type"], "AI")

        for row in result["defectResults"]:
            inspection = next(
                item
                for item in result["inspections"]
                if item["id"] == row["inspection_id"]
            )
            if inspection["final_label"] in {"PASS", "FAIL"}:
                self.assertEqual(row["label"], inspection["final_label"])
                self.assertIsNone(row["defect_type"])
                self.assertIsNone(row["bbox"])
                self.assertEqual(row["raw_response"]["defects"], [])

    def test_reject_requires_recorded_defect_case(self):
        with self.assertRaisesRegex(
            ValueError,
            "cannot synthesize a credible REJECT",
        ):
            MODULE.rebalance_fixture(
                fixture(),
                pass_cells=["SIM-0001", "SIM-0002"],
                reject_cells=["SIM-0003"],
                fail_cells=[],
            )


if __name__ == "__main__":
    unittest.main()
