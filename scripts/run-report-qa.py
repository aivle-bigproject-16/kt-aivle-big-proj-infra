#!/usr/bin/env python3
"""Trigger authenticated individual/daily reports and record VLM outcomes."""

import argparse
import hashlib
import importlib.util
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import boto3


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument(
        "--daily-only",
        action="store_true",
        help="skip individual generation when it has already been verified",
    )
    parser.add_argument("--base-url", default="http://3.36.98.158/api")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".cache/replay/report-qa.json"),
    )
    parser.add_argument(
        "--s3-prefix",
        default="simulations/server-simulation-v1.7/replay/live",
    )
    return parser.parse_args()


def load_wave_module():
    path = Path(__file__).with_name("run-qa-wave.py")
    spec = importlib.util.spec_from_file_location("run_qa_wave", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def post_json(base_url, cookie, path, body):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"report request failed: HTTP {error.code} {detail}"
        ) from error


def select_report_inputs(wave, run_id):
    with wave.connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.battery_cell_id, c.cell_serial_no
                FROM inspection i
                JOIN inspection_batch b ON b.id = i.inspection_batch_id
                JOIN battery_cell c ON c.id = i.battery_cell_id
                WHERE b.simulation_run_id = %s
                GROUP BY i.battery_cell_id, c.cell_serial_no
                HAVING count(*) = 2
                   AND bool_and(i.status = 'COMPLETED')
                   AND bool_or(i.final_label = 'REJECT')
                ORDER BY i.battery_cell_id
                LIMIT 1
                """,
                (run_id,),
            )
            cell = cursor.fetchone()
            if cell is None:
                raise RuntimeError("no completed REJECT cell is available")
            cursor.execute(
                "SELECT started_at::date FROM simulation_run WHERE id = %s",
                (run_id,),
            )
            report_date = cursor.fetchone()
            if report_date is None:
                raise RuntimeError("simulation run does not exist")
    return cell[0], cell[1], report_date[0]


def wait_for_report(wave, table, report_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    started = time.monotonic()
    last_status = None
    while time.monotonic() < deadline:
        with wave.connection() as database:
            with database.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT status, title, content, failure_reason,
                           dispatched_at, created_at, updated_at
                    FROM {table}
                    WHERE id = %s
                    """,
                    (report_id,),
                )
                row = cursor.fetchone()
        if row is None:
            raise RuntimeError(f"report {report_id} disappeared")
        if row[0] != last_status:
            print(
                f"table={table} reportId={report_id} status={row[0]}",
                flush=True,
            )
            last_status = row[0]
        if row[0] in {"COMPLETED", "FAILED"}:
            return {
                "reportId": report_id,
                "status": row[0],
                "title": row[1],
                "content": row[2],
                "contentLength": len(row[2] or ""),
                "failureReason": row[3],
                "dispatchedAt": row[4],
                "createdAt": row[5],
                "updatedAt": row[6],
                "elapsedSeconds": round(time.monotonic() - started, 3),
            }
        time.sleep(5)
    raise TimeoutError(f"report {report_id} exceeded timeout")


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"cannot encode {type(value).__name__}")


def main():
    args = parse_args()
    wave = load_wave_module()
    cookie = wave.qa_cookie()
    wave.verify_auth(args.base_url, cookie)
    cell_id, serial_no, report_date = select_report_inputs(wave, args.run_id)

    status = None
    individual = None
    if not args.daily_only:
        status, individual_response = post_json(
            args.base_url,
            cookie,
            "/reports/individual",
            {"batteryCellId": cell_id, "forceRegenerate": True},
        )
        individual_id = individual_response["data"]["reportId"]
        individual = wait_for_report(
            wave,
            "reports_individual",
            individual_id,
            args.timeout_seconds,
        )

    daily_status, daily_response = post_json(
        args.base_url,
        cookie,
        "/reports/daily",
        {"reportDate": report_date.isoformat()},
    )
    daily_id = daily_response["data"]["reportId"]
    daily = wait_for_report(
        wave,
        "reports_daily",
        daily_id,
        args.timeout_seconds,
    )

    result = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "batteryCellId": cell_id,
        "cellSerialNo": serial_no,
        "reportDate": report_date,
        "individualHttpStatus": status,
        "individual": individual,
        "dailyHttpStatus": daily_status,
        "daily": daily,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    sha_path = args.output.with_suffix(args.output.suffix + ".sha256")
    sha_path.write_text(
        f"{digest}  {args.output.name}\n",
        encoding="utf-8",
    )
    key = f"{args.s3_prefix.rstrip('/')}/{args.output.name}"
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    s3.upload_file(str(args.output), "kt-aivle-big-proj-kks", key)
    s3.upload_file(
        str(sha_path),
        "kt-aivle-big-proj-kks",
        f"{key}.sha256",
    )
    print(
        f"reportEvidenceUploaded=s3://kt-aivle-big-proj-kks/{key} "
        f"sha256={digest}",
        flush=True,
    )


if __name__ == "__main__":
    main()
