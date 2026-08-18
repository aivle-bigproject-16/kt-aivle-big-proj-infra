import hashlib
import json
import time
from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest

from app.fixture import (
    FixtureError,
    ReplayCatalog,
    ReportCatalog,
    load_verified_json,
    request_fingerprint,
)
from app.main import Settings, create_app


def analysis_fixture():
    image = {
        "id": 11,
        "inspection_id": 7,
        "image_type": "CT",
        "bucket_name": "battery",
        "object_key": "approved/ct/one.jpg",
        "source_object_key": "approved/ct/one.jpg",
        "attempt_no": 1,
    }
    fingerprint = request_fingerprint([image])
    return {
        "schemaVersion": 1,
        "mode": "LIVE_RECORD",
        "inspections": [
            {
                "id": 7,
                "cell_serial_no": "SIM-0001",
                "status": "COMPLETED",
                "final_label": "REJECT",
                "failure_type": None,
                "failure_reason": None,
            }
        ],
        "inspectionImages": [image],
        "defectResults": [
            {
                "id": 20,
                "inspection_id": 7,
                "inspection_image_id": 11,
                "image_type": "CT",
                "label": "REJECT",
                "defect_type": "CRACK",
                "confidence": "0.9100",
                "bbox": {"x": 1, "y": 2, "width": 3, "height": 4},
                "raw_response": {
                    "label": "REJECT",
                    "confidence": 0.91,
                    "defects": [
                        {
                            "defectType": "CRACK",
                            "confidence": 0.91,
                            "bbox": {
                                "x": 1,
                                "y": 2,
                                "width": 3,
                                "height": 4,
                            },
                        }
                    ],
                },
                "latency_ms": 321,
                "attempt_no": 1,
            }
        ],
        "replayIndex": [
            {
                "requestFingerprint": fingerprint,
                "inspectionId": 7,
                "attemptNo": 1,
                "imageCount": 1,
                "imageTypes": ["CT"],
            }
        ],
    }


def report_fixture():
    return {
        "schemaVersion": 1,
        "cellSerialNo": "SIM-0001",
        "reportDate": "2026-08-16",
        "individual": {
            "status": "COMPLETED",
            "title": "Cell [SIM-0001] 개별 검사 리포트",
            "content": "**Inspection ID:** 7",
            "failureReason": None,
        },
        "daily": {
            "status": "COMPLETED",
            "title": "2026-08-16 총 요약 보고서",
            "content": "2026-08-16 recorded narrative",
            "failureReason": None,
        },
    }


def pooled_analysis_fixture(cell_count=2):
    """LIVE_RECORD fixture with `cell_count` recorded CT cells."""
    inspections = []
    images = []
    results = []
    replay_index = []
    for offset in range(cell_count):
        serial = f"SIM-{offset + 1:04d}"
        inspection_id = 100 + offset
        image_id = 200 + offset
        label = "PASS" if offset % 2 == 0 else "REJECT"
        image = {
            "id": image_id,
            "inspection_id": inspection_id,
            "image_type": "CT",
            "bucket_name": "battery",
            "object_key": f"approved/ct/{serial}.jpg",
            "source_object_key": f"approved/ct/{serial}.jpg",
            "attempt_no": 1,
        }
        images.append(image)
        inspections.append({
            "id": inspection_id,
            "cell_serial_no": serial,
            "inspection_type": "CT",
            "status": "COMPLETED",
            "final_label": label,
            "failure_type": None,
            "failure_reason": None,
        })
        results.append({
            "id": 300 + offset,
            "inspection_id": inspection_id,
            "inspection_image_id": image_id,
            "image_type": "CT",
            "label": label,
            "defect_type": "CRACK" if label == "REJECT" else None,
            "confidence": "0.8800",
            "bbox": {"x": 1, "y": 2, "width": 3, "height": 4}
            if label == "REJECT"
            else None,
            "raw_response": None,
            "latency_ms": 100 + offset,
            "attempt_no": 1,
        })
        replay_index.append({
            "requestFingerprint": request_fingerprint([image]),
            "inspectionId": inspection_id,
            "attemptNo": 1,
            "imageCount": 1,
            "imageTypes": ["CT"],
        })
    return {
        "schemaVersion": 1,
        "mode": "LIVE_RECORD",
        "inspections": inspections,
        "inspectionImages": images,
        "defectResults": results,
        "replayIndex": replay_index,
    }


def pooled_report_fixture(cell_count=2):
    return {
        "schemaVersion": 2,
        "reportDate": "2026-08-16",
        "individuals": [
            {
                "cellSerialNo": f"SIM-{offset + 1:04d}",
                "inspectionId": 100 + offset,
                "sourceInspectionIds": [100 + offset],
                "report": {
                    "status": "COMPLETED",
                    "title": f"Cell [SIM-{offset + 1:04d}] 개별 검사 리포트",
                    "content": (
                        f"**대표 검사 ID:** {100 + offset}\n"
                        f"셀 SIM-{offset + 1:04d} 판정 요약"
                    ),
                    "failureReason": None,
                },
            }
            for offset in range(cell_count)
        ],
        "daily": {
            "status": "COMPLETED",
            "title": "2026-08-16 총 요약 보고서",
            "content": "2026-08-16 recorded narrative",
            "failureReason": None,
        },
    }


def settings(cell_pool_enabled=True):
    return Settings(
        fixture_uri="unused",
        fixture_sha256="a" * 64,
        report_fixture_uri="unused",
        report_fixture_sha256="b" * 64,
        aws_region="ap-northeast-2",
        internal_api_key="test-internal-key",
        backend_callback_url=(
            "http://backend:8080/internal/ai/callbacks/cell"
        ),
        delay_ms=0,
        max_pending=4,
        callback_timeout_seconds=1,
        callback_max_attempts=1,
        cell_pool_enabled=cell_pool_enabled,
    )


def request_body(
    request_id="current-request",
    object_key="approved/ct/one.jpg",
    cell_serial_no="SIM-0001",
):
    return {
        "requestId": request_id,
        "batchId": 100,
        "inspectionId": 200,
        "batteryCellId": 300,
        "cellSerialNo": cell_serial_no,
        "requestedAt": datetime.now(timezone.utc).isoformat(),
        "callbackUrl": "http://backend:8080/internal/ai/callbacks/cell",
        "images": [
            {
                "imageId": 400,
                "imageType": "CT",
                "bucketName": "battery",
                "objectKey": object_key,
            }
        ],
    }


def build_client(
    callbacks,
    cell_pool_enabled=True,
    analysis_payload=None,
    report_payload=None,
):
    async def capture_callback(url, payload, internal_key, timeout):
        callbacks.append((url, payload, internal_key, timeout))

    catalog = ReplayCatalog(analysis_payload or analysis_fixture(), "a" * 64)
    reports = ReportCatalog(report_payload or report_fixture(), "b" * 64)
    app = create_app(
        settings(cell_pool_enabled),
        catalog,
        reports,
        capture_callback,
    )
    return TestClient(app)


def wait_for_callbacks(callbacks, count):
    deadline = time.monotonic() + 1
    while len(callbacks) < count and time.monotonic() < deadline:
        time.sleep(0.01)


def test_replays_recorded_result_with_current_ids_and_is_idempotent():
    callbacks = []
    with build_client(callbacks) as client:
        response = client.post(
            "/ai/cells/analyze",
            json=request_body(),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert response.status_code == 202
        assert response.json()["inspectionId"] == 200
        wait_for_callbacks(callbacks, 1)
        assert len(callbacks) == 1

        payload = callbacks[0][1]
        assert payload["requestId"] == "current-request"
        assert payload["batchId"] == 100
        assert payload["inspectionId"] == 200
        assert payload["batteryCellId"] == 300
        assert payload["imageResults"][0]["imageId"] == 400
        assert payload["imageResults"][0]["defects"][0]["defectType"] == "CRACK"
        assert payload["finalLabel"] == "REJECT"

        duplicate = client.post(
            "/ai/cells/analyze",
            json=request_body(),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert duplicate.status_code == 202
        time.sleep(0.03)
        assert len(callbacks) == 1

        health = client.get("/health").json()
        assert health["mode"] == "REPLAY"
        assert health["metrics"]["hits"] == 1
        assert health["metrics"]["duplicates"] == 1
        assert health["metrics"]["callbackSuccess"] == 1


def test_rejects_unauthorized_miss_and_request_id_conflict():
    callbacks = []
    with build_client(callbacks, cell_pool_enabled=False) as client:
        assert client.post("/ai/cells/analyze", json=request_body()).status_code == 401
        wrong_callback = request_body(request_id="wrong-callback")
        wrong_callback["callbackUrl"] = "http://untrusted/callback"
        assert client.post(
            "/ai/cells/analyze",
            json=wrong_callback,
            headers={"X-Internal-Api-Key": "test-internal-key"},
        ).status_code == 400
        miss = client.post(
            "/ai/cells/analyze",
            json=request_body(object_key="not-recorded.jpg"),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert miss.status_code == 404

        accepted = client.post(
            "/ai/cells/analyze",
            json=request_body(),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert accepted.status_code == 202
        conflict_body = request_body()
        conflict_body["inspectionId"] = 201
        conflict = client.post(
            "/ai/cells/analyze",
            json=conflict_body,
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert conflict.status_code == 409


def test_replays_only_recorded_individual_report_and_maps_dynamic_fields():
    callbacks = []
    with build_client(callbacks, cell_pool_enabled=False) as client:
        individual = client.post(
            "/vlm/reports/individual",
            json={
                "cellSerialNo": "SIM-0001",
                "inspectionId": 999,
                "totalImages": 1,
                "cellSize": None,
                "pointGroups": [],
                "ctVoidRatio": None,
                "rgbDefectRate": None,
                "defectInfo": [],
            },
        )
        assert individual.status_code == 200
        assert "999" in individual.json()["content"]

        missing = client.post(
            "/vlm/reports/individual",
            json={
                "cellSerialNo": "SIM-0002",
                "inspectionId": 1000,
                "totalImages": 1,
                "cellSize": None,
                "pointGroups": [],
                "ctVoidRatio": None,
                "rgbDefectRate": None,
                "defectInfo": [],
            },
        )
        assert missing.status_code == 404

        daily = client.post(
            "/vlm/reports/daily",
            json={
                "daily_data": {
                    "reportDate": "2026-08-17",
                    "summaryData": {
                        "totalCount": 40,
                        "passCount": 25,
                        "rejectCount": 15,
                        "failedCount": 0,
                        "prevTotalCount": 180,
                        "prevRejectCount": 73,
                        "defects": [
                            {"defectType": "CRACK", "count": 1254},
                            {"defectType": "SPOT", "count": 936},
                        ],
                    },
                }
            },
        )
        assert daily.status_code == 200
        assert "2026-08-17" in daily.json()["title"]
        assert "|총 검사 수|40건|" in daily.json()["content"]
        assert "|양품 (PASS)|25건|" in daily.json()["content"]
        assert "|불량 (REJECT)|15건|" in daily.json()["content"]
        assert "140" not in daily.json()["content"]

        invalid_daily = client.post(
            "/vlm/reports/daily",
            json={"daily_data": {"reportDate": "2026-08-17"}},
        )
        assert invalid_daily.status_code == 422


def test_report_catalog_v2_maps_multiple_cells_and_source_inspection_ids():
    payload = report_fixture()
    payload["schemaVersion"] = 2
    payload.pop("cellSerialNo")
    payload.pop("individual")
    payload["individuals"] = [
        {
            "cellSerialNo": "SIM-0001",
            "inspectionId": 7,
            "sourceInspectionIds": [7, 8],
            "report": {
                "status": "COMPLETED",
                "title": "Cell [SIM-0001] 개별 검사 리포트",
                "content": "**대표 검사 ID:** 7\n**연결 검사 ID:** [7, 8]",
                "failureReason": None,
            },
        },
        {
            "cellSerialNo": "SIM-0002",
            "inspectionId": 9,
            "sourceInspectionIds": [9, 10],
            "report": {
                "status": "COMPLETED",
                "title": "Cell [SIM-0002] 개별 검사 리포트",
                "content": "**대표 검사 ID:** 9\n**연결 검사 ID:** [9, 10]",
                "failureReason": None,
            },
        },
    ]

    catalog = ReportCatalog(payload, "b" * 64)
    response = catalog.individual_response("SIM-0002", 109, [109, 110])

    assert response.title == "Cell [SIM-0002] 개별 검사 리포트"
    assert "**대표 검사 ID:** 109" in response.content
    assert "**연결 검사 ID:** [109, 110]" in response.content
    assert "10109" not in response.content


def test_analysis_fixture_digest_matches_serialized_bytes():
    payload = analysis_fixture()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(hashlib.sha256(data).hexdigest()) == 64


def test_fixture_loader_fails_closed_on_sha_mismatch(tmp_path):
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps(analysis_fixture()), encoding="utf-8")
    with pytest.raises(FixtureError, match="SHA-256 mismatch"):
        load_verified_json(str(fixture), "0" * 64, "ap-northeast-2")


def pooled_client(callbacks, cell_count=2):
    return build_client(
        callbacks,
        analysis_payload=pooled_analysis_fixture(cell_count),
        report_payload=pooled_report_fixture(cell_count),
    )


def test_cell_pool_replays_unrecorded_cells_by_cycling_recorded_group():
    callbacks = []
    with pooled_client(callbacks) as client:
        for index in range(5):
            response = client.post(
                "/ai/cells/analyze",
                json=request_body(
                    request_id=f"pool-request-{index}",
                    object_key=f"live/ct/cell-{index}.jpg",
                    cell_serial_no=f"SIM-9{index:03d}",
                ),
                headers={"X-Internal-Api-Key": "test-internal-key"},
            )
            assert response.status_code == 202

        wait_for_callbacks(callbacks, 5)
        assert len(callbacks) == 5

        labels = [payload["finalLabel"] for _, payload, _, _ in callbacks]
        assert labels == ["PASS", "REJECT", "PASS", "REJECT", "PASS"]

        serials = [payload["cellSerialNo"] for _, payload, _, _ in callbacks]
        assert serials == [f"SIM-9{index:03d}" for index in range(5)]
        assert all(
            payload["imageResults"][0]["imageId"] == 400
            for _, payload, _, _ in callbacks
        )

        health = client.get("/health").json()
        assert health["cellPool"]["enabled"] is True
        assert health["cellPool"]["groupSize"] == 2
        assert health["cellPool"]["assignedCells"] == 5
        assert health["cellPool"]["cycles"] == 2
        assert health["metrics"]["poolHits"] == 5
        assert health["metrics"]["misses"] == 0


def test_cell_pool_keeps_one_slot_per_cell_across_requests():
    callbacks = []
    with pooled_client(callbacks) as client:
        for attempt in range(2):
            response = client.post(
                "/ai/cells/analyze",
                json=request_body(
                    request_id=f"recapture-{attempt}",
                    object_key=f"live/ct/recapture-{attempt}.jpg",
                    cell_serial_no="SIM-9100",
                ),
                headers={"X-Internal-Api-Key": "test-internal-key"},
            )
            assert response.status_code == 202

        wait_for_callbacks(callbacks, 2)
        assert len(callbacks) == 2
        assert {payload["finalLabel"] for _, payload, _, _ in callbacks} == {"PASS"}

        health = client.get("/health").json()
        assert health["cellPool"]["assignedCells"] == 1
        assert health["cellPool"]["cycles"] == 0


def test_cell_pool_maps_individual_report_to_assigned_slot():
    callbacks = []
    with pooled_client(callbacks) as client:
        analyze = client.post(
            "/ai/cells/analyze",
            json=request_body(
                request_id="report-cell",
                object_key="live/ct/report-cell.jpg",
                cell_serial_no="SIM-9200",
            ),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert analyze.status_code == 202
        wait_for_callbacks(callbacks, 1)

        report = client.post(
            "/vlm/reports/individual",
            json={
                "cellSerialNo": "SIM-9200",
                "inspectionId": 777,
                "totalImages": 1,
                "cellSize": None,
                "pointGroups": [],
                "ctVoidRatio": None,
                "rgbDefectRate": None,
                "defectInfo": [],
                "sourceInspectionIds": [777],
            },
        )
        assert report.status_code == 200
        body = report.json()
        assert "SIM-9200" in body["title"]
        assert "SIM-9200" in body["content"]
        assert "SIM-0001" not in body["content"]
        assert "**대표 검사 ID:** 777" in body["content"]


def test_cell_pool_disabled_keeps_fail_closed_miss():
    callbacks = []
    with build_client(
        callbacks,
        cell_pool_enabled=False,
        analysis_payload=pooled_analysis_fixture(),
        report_payload=pooled_report_fixture(),
    ) as client:
        response = client.post(
            "/ai/cells/analyze",
            json=request_body(
                request_id="unrecorded",
                object_key="live/ct/unrecorded.jpg",
                cell_serial_no="SIM-9999",
            ),
            headers={"X-Internal-Api-Key": "test-internal-key"},
        )
        assert response.status_code == 404
        assert client.get("/health").json()["metrics"]["misses"] == 1


def test_pooled_report_ids_do_not_rewrite_serial_digits():
    payload = pooled_report_fixture(1)
    entry = payload["individuals"][0]
    entry["inspectionId"] = 1
    entry["sourceInspectionIds"] = [1]
    entry["report"]["content"] = (
        "**대표 검사 ID:** 1\n**연결 검사 ID:** [1]\nSIM-0001 판정 요약"
    )
    catalog = ReportCatalog(payload, "b" * 64)

    response = catalog.individual_response(
        "SIM-RUN-0100",
        424242,
        [424242],
        "SIM-0001",
    )

    assert "**대표 검사 ID:** 424242" in response.content
    assert "**연결 검사 ID:** [424242]" in response.content
    assert "SIM-RUN-0100 판정 요약" in response.content
    assert "SIM-RUN-042424200" not in response.content
