#!/usr/bin/env python3
"""Export durable LIVE simulation evidence for a future CPU replay service."""

import argparse
import hashlib
import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, action="append", required=True)
    parser.add_argument("--wave-no", type=int, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def connection_string():
    return os.getenv("DB_URL", DEFAULT_DB_URL).removeprefix("jdbc:")


def fetch_all(cursor, query, parameters):
    cursor.execute(query, parameters)
    return cursor.fetchall()


def json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"cannot encode {type(value).__name__}")


def request_fingerprint(images):
    identity = "\n".join(sorted(
        f"{image['image_type']}|{image['bucket_name']}|{image['object_key']}"
        for image in images
    ))
    return hashlib.sha256(identity.encode()).hexdigest()


def export_fixture(run_ids, wave_no, manifest_sha256):
    with psycopg.connect(
        connection_string(),
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            runs = fetch_all(
                cursor,
                """
                SELECT id, batch_count, cells_per_batch, battery_cell_count,
                       interval_ms, status, started_at, ended_at
                FROM simulation_run
                WHERE id = ANY(%s)
                ORDER BY id
                """,
                (run_ids,),
            )
            if len(runs) != len(set(run_ids)):
                raise RuntimeError("one or more simulation runs do not exist")

            inspections = fetch_all(
                cursor,
                """
                SELECT i.id, i.inspection_batch_id, b.simulation_run_id,
                       i.battery_cell_id, c.cell_serial_no,
                       i.inspection_type, i.status, i.final_label,
                       i.failure_type, i.failure_reason,
                       i.capture_retry_count, i.ai_request_id,
                       i.created_at, i.updated_at, i.analyzed_at
                FROM inspection i
                JOIN inspection_batch b ON b.id = i.inspection_batch_id
                JOIN battery_cell c ON c.id = i.battery_cell_id
                WHERE b.simulation_run_id = ANY(%s)
                ORDER BY b.simulation_run_id, i.id
                """,
                (run_ids,),
            )
            inspection_ids = [row["id"] for row in inspections]

            images = fetch_all(
                cursor,
                """
                SELECT ii.id, ii.inspection_id, ii.image_type,
                       ii.bucket_name, ii.object_key, ii.source_object_key,
                       ii.attempt_no, ii.created_at
                FROM inspection_image ii
                WHERE ii.inspection_id = ANY(%s)
                ORDER BY ii.inspection_id, ii.attempt_no, ii.id
                """,
                (inspection_ids,),
            ) if inspection_ids else []

            results = fetch_all(
                cursor,
                """
                SELECT dr.id, dr.inspection_id, dr.inspection_image_id,
                       dr.image_type, dr.label, dr.defect_type,
                       dr.confidence, dr.bbox, dr.raw_response,
                       dr.latency_ms, dr.attempt_no, dr.ai_request_id,
                       dr.created_at
                FROM defect_result dr
                WHERE dr.inspection_id = ANY(%s)
                ORDER BY dr.inspection_id, dr.attempt_no, dr.id
                """,
                (inspection_ids,),
            ) if inspection_ids else []

            api_logs = fetch_all(
                cursor,
                """
                SELECT id, direction, endpoint, inspection_id, http_status,
                       latency_ms, request_body, response_body,
                       error_message, created_at
                FROM api_log
                WHERE inspection_id = ANY(%s)
                ORDER BY id
                """,
                (inspection_ids,),
            ) if inspection_ids else []

            individual_reports = fetch_all(
                cursor,
                """
                SELECT id, version, battery_cell_id,
                       representative_inspection_id, source_inspection_ids,
                       status, title, content, failure_reason,
                       dispatched_at, created_at, updated_at
                FROM reports_individual
                WHERE representative_inspection_id = ANY(%s)
                ORDER BY id
                """,
                (inspection_ids,),
            ) if inspection_ids else []

            daily_reports = fetch_all(
                cursor,
                """
                SELECT id, report_date, status, title, summary_json,
                       content, failure_reason, dispatched_at,
                       created_at, updated_at
                FROM reports_daily
                WHERE report_date = ANY(
                    SELECT DISTINCT started_at::date
                    FROM simulation_run
                    WHERE id = ANY(%s)
                )
                ORDER BY id
                """,
                (run_ids,),
            )

    images_by_inspection = {}
    for image in images:
        images_by_inspection.setdefault(image["inspection_id"], []).append(image)

    replay_index = []
    for inspection in inspections:
        inspection_images = images_by_inspection.get(inspection["id"], [])
        by_attempt = {}
        for image in inspection_images:
            by_attempt.setdefault(image["attempt_no"], []).append(image)
        for attempt_no, attempt_images in sorted(by_attempt.items()):
            replay_index.append({
                "requestFingerprint": request_fingerprint(attempt_images),
                "inspectionId": inspection["id"],
                "attemptNo": attempt_no,
                "imageCount": len(attempt_images),
                "imageTypes": sorted({
                    image["image_type"] for image in attempt_images
                }),
            })

    return {
        "schemaVersion": 1,
        "mode": "LIVE_RECORD",
        "waveNo": wave_no,
        "manifestSha256": manifest_sha256,
        "exportedAt": datetime.now().astimezone().isoformat(),
        "runIds": run_ids,
        "runs": runs,
        "inspections": inspections,
        "inspectionImages": images,
        "defectResults": results,
        "apiLogs": api_logs,
        "individualReports": individual_reports,
        "dailyReports": daily_reports,
        "replayIndex": replay_index,
    }


def main():
    args = parse_args()
    fixture = export_fixture(
        args.run_id,
        args.wave_no,
        args.manifest_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            fixture,
            ensure_ascii=False,
            indent=2,
            default=json_default,
        ),
        encoding="utf-8",
    )
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(
        f"{digest}  {args.output.name}\n",
        encoding="utf-8",
    )
    print(
        f"wrote {args.output}: runs={len(fixture['runs'])} "
        f"inspections={len(fixture['inspections'])} "
        f"results={len(fixture['defectResults'])} sha256={digest}"
    )


if __name__ == "__main__":
    main()
