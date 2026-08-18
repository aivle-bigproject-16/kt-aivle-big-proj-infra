from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import boto3

from app.models import (
    CallbackBoundingBox,
    CallbackDefect,
    CellAnalysisCallback,
    CellAnalysisRequest,
    DailyReportData,
    ImageAnalysisResult,
    ReportResponse,
)


MAX_FIXTURE_BYTES = 64 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
StableImageKey = tuple[str, str, str]


class FixtureError(RuntimeError):
    pass


class FixtureMiss(LookupError):
    pass


def stable_image_key(image: Any) -> StableImageKey:
    if isinstance(image, dict):
        return (
            str(image["image_type"]),
            str(image["bucket_name"]),
            str(image["object_key"]),
        )
    return (image.image_type, image.bucket_name, image.object_key)


def request_modality(images: Iterable[Any]) -> str:
    types = sorted({
        str(image["image_type"]) if isinstance(image, dict) else image.image_type
        for image in images
    })
    return "+".join(types)


def request_fingerprint(images: Iterable[Any]) -> str:
    identities = ["|".join(stable_image_key(image)) for image in images]
    if len(identities) != len(set(identities)):
        raise FixtureError("duplicate stable image key")
    identity = "\n".join(sorted(identities))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _read_fixture_bytes(uri: str, region: str) -> bytes:
    if os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", uri):
        data = Path(uri).read_bytes()
        if len(data) > MAX_FIXTURE_BYTES:
            raise FixtureError("fixture exceeds 64 MiB limit")
        return data

    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if not parsed.netloc or not parsed.path.strip("/"):
            raise FixtureError(f"invalid S3 fixture URI: {uri}")
        response = boto3.client("s3", region_name=region).get_object(
            Bucket=parsed.netloc,
            Key=parsed.path.lstrip("/"),
        )
        if int(response.get("ContentLength") or 0) > MAX_FIXTURE_BYTES:
            response["Body"].close()
            raise FixtureError("fixture exceeds 64 MiB limit")
        try:
            data = response["Body"].read(MAX_FIXTURE_BYTES + 1)
        finally:
            response["Body"].close()
    elif parsed.scheme == "file":
        data = Path(unquote(parsed.path)).read_bytes()
    elif not parsed.scheme:
        data = Path(uri).read_bytes()
    else:
        raise FixtureError(f"unsupported fixture URI scheme: {parsed.scheme}")

    if len(data) > MAX_FIXTURE_BYTES:
        raise FixtureError("fixture exceeds 64 MiB limit")
    return data


def load_verified_json(
    uri: str,
    expected_sha256: str,
    region: str,
) -> tuple[dict[str, Any], str]:
    expected = expected_sha256.strip().lower()
    if not SHA256_PATTERN.fullmatch(expected):
        raise FixtureError("expected fixture SHA-256 must be 64 lowercase hex")

    data = _read_fixture_bytes(uri, region)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise FixtureError(
            f"fixture SHA-256 mismatch: expected={expected} actual={actual}"
        )
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureError("fixture is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise FixtureError("fixture root must be a JSON object")
    return payload, actual


@dataclass(frozen=True)
class RecordedAnalysis:
    inspection: dict[str, Any]
    images_by_key: dict[StableImageKey, dict[str, Any]]
    results_by_image_id: dict[int, list[dict[str, Any]]]
    fingerprint: str
    cell_serial_no: str = ""
    modality: str = ""
    attempt_no: int = 1


CellPoolIndex = dict[str, dict[str, dict[int, "RecordedAnalysis"]]]


def _index_cell_pool(
    inspections: list[dict[str, Any]],
    recorded_entries: Iterable["RecordedAnalysis"],
) -> tuple[list[str], CellPoolIndex]:
    """Group recorded entries by logical cell, keeping fixture order."""
    index: CellPoolIndex = {}
    for recorded in recorded_entries:
        by_modality = index.setdefault(recorded.cell_serial_no, {})
        attempts = by_modality.setdefault(recorded.modality, {})
        attempts[recorded.attempt_no] = recorded

    ordered: list[str] = []
    for inspection in inspections:
        serial = str(inspection.get("cell_serial_no") or "")
        if serial in index and serial not in ordered:
            ordered.append(serial)
    for serial in index:
        if serial not in ordered:
            ordered.append(serial)
    return ordered, index


class ReplayCatalog:
    def __init__(self, payload: dict[str, Any], digest: str):
        if payload.get("schemaVersion") != 1:
            raise FixtureError("analysis fixture schemaVersion must be 1")
        if payload.get("mode") != "LIVE_RECORD":
            raise FixtureError("analysis fixture mode must be LIVE_RECORD")
        for field in (
            "inspections",
            "inspectionImages",
            "defectResults",
            "replayIndex",
        ):
            if not isinstance(payload.get(field), list):
                raise FixtureError(f"analysis fixture field {field} must be a list")

        inspections = {int(row["id"]): row for row in payload["inspections"]}
        images_by_inspection: dict[int, list[dict[str, Any]]] = {}
        for image in payload["inspectionImages"]:
            images_by_inspection.setdefault(
                int(image["inspection_id"]), []
            ).append(image)
        results_by_image: dict[int, list[dict[str, Any]]] = {}
        for result in payload["defectResults"]:
            image_id = result.get("inspection_image_id")
            if image_id is not None:
                results_by_image.setdefault(int(image_id), []).append(result)

        entries: dict[str, RecordedAnalysis] = {}
        for index in payload["replayIndex"]:
            inspection_id = int(index["inspectionId"])
            attempt_no = int(index["attemptNo"])
            inspection = inspections.get(inspection_id)
            if inspection is None:
                raise FixtureError(
                    f"replay index references missing inspection {inspection_id}"
                )
            images = [
                image
                for image in images_by_inspection.get(inspection_id, [])
                if int(image["attempt_no"]) == attempt_no
            ]
            if len(images) != int(index["imageCount"]):
                raise FixtureError(
                    f"image count mismatch for inspection {inspection_id}"
                )
            calculated = request_fingerprint(images)
            recorded = str(index["requestFingerprint"])
            if calculated != recorded:
                raise FixtureError(
                    f"fingerprint mismatch for inspection {inspection_id}"
                )
            if recorded in entries:
                raise FixtureError(f"duplicate replay fingerprint {recorded}")
            modality = str(
                inspection.get("inspection_type") or request_modality(images)
            )
            images_by_key = {stable_image_key(image): image for image in images}
            selected_results: dict[int, list[dict[str, Any]]] = {}
            for image in images:
                image_id = int(image["id"])
                rows = results_by_image.get(image_id, [])
                if not rows:
                    raise FixtureError(
                        f"missing result for inspection image {image_id}"
                    )
                selected_results[image_id] = rows
            entries[recorded] = RecordedAnalysis(
                inspection=inspection,
                images_by_key=images_by_key,
                results_by_image_id=selected_results,
                fingerprint=recorded,
                cell_serial_no=str(inspection["cell_serial_no"]),
                modality=modality,
                attempt_no=attempt_no,
            )

        if not entries:
            raise FixtureError("analysis fixture contains no replay entries")
        self.digest = digest
        self.entries = entries
        self.cell_serials, self.entries_by_cell = _index_cell_pool(
            payload["inspections"],
            entries.values(),
        )

    def match(self, request: CellAnalysisRequest) -> RecordedAnalysis:
        try:
            fingerprint = request_fingerprint(request.images)
        except FixtureError as exc:
            raise FixtureMiss(str(exc)) from exc
        recorded = self.entries.get(fingerprint)
        if recorded is None:
            raise FixtureMiss(f"fixture fingerprint miss: {fingerprint}")
        if str(recorded.inspection["cell_serial_no"]) != request.cell_serial_no:
            raise FixtureMiss(
                "fixture cell serial mismatch for fingerprint " + fingerprint
            )
        return recorded

    def entry_for(
        self,
        cell_serial_no: str,
        modality: str,
        attempt_index: int,
    ) -> RecordedAnalysis:
        """Return a recorded entry for a pooled cell slot.

        `attempt_index` is the zero-based count of analyses already replayed
        for the requesting cell and modality, so a recapture consumes the
        recorded attempt 2 when the fixture holds one.
        """
        by_modality = self.entries_by_cell.get(cell_serial_no)
        if not by_modality:
            raise FixtureMiss(f"pool slot has no recorded cell {cell_serial_no}")
        attempts = by_modality.get(modality)
        if attempts is None:
            requested_types = set(modality.split("+"))
            overlapping = sorted(
                candidate
                for candidate in by_modality
                if set(candidate.split("+")) & requested_types
            )
            if not overlapping:
                raise FixtureMiss(
                    f"pool slot {cell_serial_no} has no {modality} recording"
                )
            attempts = by_modality[overlapping[0]]
        ordered = [attempts[number] for number in sorted(attempts)]
        return ordered[min(attempt_index, len(ordered) - 1)]

    def build_callback(
        self,
        recorded: RecordedAnalysis,
        request: CellAnalysisRequest,
        completed_at,
    ) -> CellAnalysisCallback:
        image_results = []
        substitutes = _recorded_images_by_type(recorded)
        consumed: dict[str, int] = {}
        for current_image in request.images:
            key = stable_image_key(current_image)
            historical_image = recorded.images_by_key.get(key)
            if historical_image is None:
                historical_image = _substitute_image(
                    substitutes,
                    consumed,
                    current_image.image_type,
                )
            if historical_image is None:
                raise FixtureError(f"matched fixture lost image key {key}")
            rows = recorded.results_by_image_id[int(historical_image["id"])]
            image_results.append(_build_image_result(current_image, rows))

        status = str(recorded.inspection["status"])
        failed = status == "FAILED"
        final_label = None if failed else recorded.inspection.get("final_label")
        if not failed and final_label not in {"PASS", "REJECT"}:
            raise FixtureError("completed fixture inspection has invalid final label")
        failure_type = (
            _normalize_failure_type(recorded.inspection.get("failure_type"))
            if failed
            else None
        )
        failure_reason = (
            str(recorded.inspection.get("failure_reason") or "recorded failure")
            if failed
            else None
        )
        confidence = 0.0 if failed else _cell_confidence(
            str(final_label), image_results
        )
        return CellAnalysisCallback(
            request_id=request.request_id,
            batch_id=request.batch_id,
            inspection_id=request.inspection_id,
            battery_cell_id=request.battery_cell_id,
            cell_serial_no=request.cell_serial_no,
            cell_status="FAILED" if failed else "COMPLETED",
            final_label=final_label,
            failure_type=failure_type,
            failure_reason=failure_reason,
            confidence=confidence,
            completed_at=completed_at,
            image_results=image_results,
        )


def _recorded_images_by_type(
    recorded: RecordedAnalysis,
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for image in recorded.images_by_key.values():
        grouped.setdefault(str(image["image_type"]), []).append(image)
    for images in grouped.values():
        images.sort(key=lambda row: (str(row["object_key"]), int(row["id"])))
    return grouped


def _substitute_image(
    grouped: dict[str, list[dict[str, Any]]],
    consumed: dict[str, int],
    image_type: str,
) -> dict[str, Any] | None:
    """Pick a recorded image for a request whose object keys are not recorded.

    Images of the same modality are consumed in a stable order and wrap around
    when the request carries more images than the recording.
    """
    candidates = grouped.get(image_type)
    if not candidates:
        candidates = [image for images in grouped.values() for image in images]
        candidates.sort(key=lambda row: (str(row["object_key"]), int(row["id"])))
    if not candidates:
        return None
    position = consumed.get(image_type, 0)
    consumed[image_type] = position + 1
    return candidates[position % len(candidates)]


def _normalize_raw_response(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _bbox(value: dict[str, Any]) -> CallbackBoundingBox:
    return CallbackBoundingBox(
        x=round(float(value["x"])),
        y=round(float(value["y"])),
        width=max(0, round(float(value["width"]))),
        height=max(0, round(float(value["height"]))),
    )


def _build_defects(
    raw: dict[str, Any] | None,
    rows: list[dict[str, Any]],
) -> list[CallbackDefect]:
    raw_defects = raw.get("defects") if raw else None
    if isinstance(raw_defects, list):
        return [
            CallbackDefect(
                defect_type=defect["defectType"],
                confidence=float(defect["confidence"]),
                bbox=_bbox(defect["bbox"]),
            )
            for defect in raw_defects
        ]
    return [
        CallbackDefect(
            defect_type=row["defect_type"],
            confidence=float(row["confidence"]),
            bbox=_bbox(row["bbox"]),
        )
        for row in rows
        if row.get("defect_type") and row.get("bbox")
    ]


def _build_image_result(current_image, rows) -> ImageAnalysisResult:
    first = rows[0]
    raw = _normalize_raw_response(first.get("raw_response"))
    label = str((raw or {}).get("label") or first["label"])
    confidence = float(
        (raw or {}).get("confidence")
        if (raw or {}).get("confidence") is not None
        else first["confidence"]
    )
    latency_ms = max(int(row.get("latency_ms") or 0) for row in rows)
    return ImageAnalysisResult(
        image_id=current_image.image_id,
        image_type=current_image.image_type,
        label=label,
        confidence=confidence,
        defects=_build_defects(raw, rows),
        raw_response=raw,
        latency_ms=latency_ms,
    )


def _cell_confidence(
    final_label: str,
    results: list[ImageAnalysisResult],
) -> float:
    decisive = [
        result.confidence for result in results if result.label == final_label
    ]
    if not decisive:
        return 0.0
    return min(decisive) if final_label == "PASS" else max(decisive)


def _normalize_failure_type(value: Any) -> str:
    return "CAPTURE" if "CAPTURE" in str(value or "").upper() else "AI"


class ReportCatalog:
    def __init__(self, payload: dict[str, Any], digest: str):
        version = payload.get("schemaVersion")
        if version not in {1, 2}:
            raise FixtureError("report fixture schemaVersion must be 1 or 2")
        if not isinstance(payload.get("daily"), dict):
            raise FixtureError("report fixture is missing daily response")
        self.digest = digest
        self.report_date = str(payload.get("reportDate") or "")
        self.daily = payload["daily"]
        if version == 1:
            if not isinstance(payload.get("individual"), dict):
                raise FixtureError("report fixture is missing individual response")
            cell_serial_no = str(payload.get("cellSerialNo") or "")
            entries = [{
                "cellSerialNo": cell_serial_no,
                "inspectionId": payload.get("individual", {}).get(
                    "inspectionId", payload.get("inspectionId")
                ),
                "sourceInspectionIds": [],
                "report": payload["individual"],
            }]
        else:
            entries = payload.get("individuals")
            if not isinstance(entries, list) or not entries:
                raise FixtureError("report fixture is missing individual responses")
        self.individuals = {
            str(entry.get("cellSerialNo") or ""): entry
            for entry in entries
        }
        if "" in self.individuals or len(self.individuals) != len(entries):
            raise FixtureError("individual report cells must be unique and non-empty")
        self.cell_serials = list(self.individuals)

    def individual_response(
        self,
        cell_serial_no: str,
        inspection_id: int | None,
        source_inspection_ids: list[int] | None = None,
        recorded_cell_serial_no: str | None = None,
    ) -> ReportResponse:
        recorded_serial = recorded_cell_serial_no or cell_serial_no
        entry = self.individuals.get(recorded_serial)
        if entry is None:
            raise FixtureMiss(
                f"individual report fixture miss for cell {recorded_serial}"
            )
        report = entry.get("report") or entry.get("individual")
        if not isinstance(report, dict):
            raise FixtureError(f"individual report is invalid for {recorded_serial}")
        title = _replace(report.get("title"), recorded_serial, cell_serial_no)
        content = _replace(
            report.get("content"), recorded_serial, cell_serial_no
        )
        recorded_sources = [
            int(value) for value in (entry.get("sourceInspectionIds") or [])
        ]
        current_sources = [int(value) for value in (source_inspection_ids or [])]
        content = _replace_inspection_id_list(
            content, recorded_sources, current_sources
        )
        if inspection_id is not None:
            old_id = entry.get("inspectionId")
            if old_id is None:
                match = re.search(r"(?:Inspection|검사) ID:\*\*\s*(\d+)", content or "")
                old_id = match.group(1) if match else None
            if old_id is not None:
                content = _replace_representative_inspection_id(
                    content, int(old_id), int(inspection_id)
                )
        return _report_response(report, title, content)

    def daily_response(self, daily_data: DailyReportData) -> ReportResponse:
        requested_date = daily_data.reportDate
        summary = daily_data.summaryData
        title = _replace(self.daily.get("title"), self.report_date, requested_date)
        yield_rate = (
            round(summary.passCount * 100 / summary.totalCount, 1)
            if summary.totalCount
            else 0.0
        )
        defects = sorted(
            summary.defects,
            key=lambda item: (-item.count, item.defectType),
        )
        defect_lines = [
            f"- **{item.defectType}:** {item.count:,}건"
            for item in defects
        ] or ["- 기록된 결함 없음"]
        content = "\n".join(
            [
                f"# {requested_date} 생산 및 수율 요약",
                "",
                "|항목|현재 집계|",
                "|---|---|",
                f"|총 검사 수|{summary.totalCount:,}건|",
                f"|양품 (PASS)|{summary.passCount:,}건|",
                f"|불량 (REJECT)|{summary.rejectCount:,}건|",
                f"|분석 실패|{summary.failedCount:,}건|",
                f"|수율|{yield_rate:.1f}%|",
                f"|전일 총 검사 수|{summary.prevTotalCount:,}건|",
                f"|전일 불량 수|{summary.prevRejectCount:,}건|",
                "",
                "### 주요 결함 발생 현황",
                *defect_lines,
                "",
                "> REPLAY 모드에서는 현재 요청의 확정 집계를 결정적으로 표시합니다.",
            ]
        )
        return _report_response(self.daily, title, content)


def _replace(value: Any, old: str, new: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text.replace(old, new) if old else text


def _replace_inspection_id_list(
    content: str | None,
    recorded: list[int],
    current: list[int],
) -> str | None:
    if content is None or not recorded or len(recorded) != len(current):
        return content
    mapping = {
        str(old): str(new)
        for old, new in zip(recorded, current)
    }
    # Digit boundaries keep a short recorded id from rewriting digits that
    # belong to another number, such as the cell serial of a pooled replay.
    pattern = re.compile(
        r"(?<![0-9])("
        + "|".join(
            re.escape(value)
            for value in sorted(mapping, key=len, reverse=True)
        )
        + r")(?![0-9])"
    )
    return pattern.sub(lambda match: mapping[match.group(1)], content)


def _replace_representative_inspection_id(
    content: str | None,
    recorded: int,
    current: int,
) -> str | None:
    if content is None or recorded == current:
        return content
    pattern = re.compile(
        rf"((?:\*\*)?(?:대표\s*)?(?:Inspection|검사) ID:(?:\*\*)?\s*){recorded}\b",
        re.IGNORECASE,
    )
    return pattern.sub(lambda match: f"{match.group(1)}{current}", content)


def _report_response(row, title, content) -> ReportResponse:
    return ReportResponse(
        status=str(row.get("status") or "FAILED"),
        title=title,
        content=content,
        failureReason=row.get("failureReason"),
    )
