#!/usr/bin/env python3
"""Seed, run, monitor, record, and upload one 20-case QA wave."""

import argparse
import hashlib
import importlib.util
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import jwt
import psycopg


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--base-url", default="http://3.36.98.158/api")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--battery-cell-count", type=int, default=20)
    parser.add_argument("--capture-speed", type=int, default=1)
    parser.add_argument(
        "--seed-version-prefix",
        help="override the DB seed migration ledger prefix",
    )
    parser.add_argument("--timeout-seconds", type=int, default=7200)
    parser.add_argument("--auth-probe-only", action="store_true")
    parser.add_argument(
        "--resume-run-id",
        type=int,
        help="resume monitoring an already-started run without reseeding",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=Path(".cache/replay"),
    )
    parser.add_argument(
        "--fixture-prefix",
        default="simulations/server-simulation-v1.7/replay/live",
    )
    return parser.parse_args()


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connection(max_attempts=5):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return psycopg.connect(
                os.getenv("DB_URL", DEFAULT_DB_URL).removeprefix("jdbc:"),
                user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
                password=os.environ["DB_PASSWORD"],
            )
        except psycopg.OperationalError as error:
            last_error = error
            if attempt == max_attempts:
                break
            time.sleep(min(attempt * 2, 10))
    raise last_error


def jwt_algorithm(secret):
    bits = len(secret.encode()) * 8
    if bits >= 512:
        return "HS512"
    if bits >= 384:
        return "HS384"
    return "HS256"


def qa_cookie():
    with connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                "SELECT email FROM users ORDER BY id LIMIT 1"
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("no existing user is available for QA authentication")
    now = datetime.now(timezone.utc)
    secret = os.environ["JWT_SECRET"]
    token = jwt.encode(
        {
            "sub": row[0],
            "iat": now,
            "exp": now + timedelta(hours=2),
        },
        secret,
        algorithm=jwt_algorithm(secret),
    )
    return f"access_token={token}"


def start_simulation(
    base_url,
    cookie,
    batch_size,
    battery_cell_count,
    capture_speed,
):
    body = json.dumps({
        "batchSize": batch_size,
        "batteryCellCount": battery_cell_count,
        "captureSpeed": capture_speed,
    }).encode()
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/sim",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": cookie,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")
        raise RuntimeError(
            f"simulation start failed: HTTP {error.code} {detail}"
        ) from error
    print(f"startResponse={json.dumps(payload, ensure_ascii=False)}", flush=True)


def verify_auth(base_url, cookie):
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/sim",
        headers={"Cookie": cookie},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise RuntimeError("QA authentication was rejected") from error
        status = error.code
    print(f"authProbeStatus={status}", flush=True)


def latest_running_run():
    with connection() as database:
        with database.cursor() as cursor:
            cursor.execute(
                """
                SELECT id FROM simulation_run
                WHERE status = 'RUNNING'
                ORDER BY started_at DESC
                LIMIT 1
                """
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("start returned but no RUNNING simulation exists")
    return row[0]


def wait_for_run(run_id, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    last_summary = None
    while time.monotonic() < deadline:
        with connection() as database:
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT status FROM simulation_run WHERE id = %s",
                    (run_id,),
                )
                run_status = cursor.fetchone()[0]
                cursor.execute(
                    """
                    SELECT i.status, count(*)
                    FROM inspection i
                    JOIN inspection_batch b ON b.id = i.inspection_batch_id
                    WHERE b.simulation_run_id = %s
                    GROUP BY i.status
                    ORDER BY i.status
                    """,
                    (run_id,),
                )
                counts = dict(cursor.fetchall())
        summary = (run_status, tuple(sorted(counts.items())))
        if summary != last_summary:
            print(
                f"runId={run_id} status={run_status} inspections={counts}",
                flush=True,
            )
            last_summary = summary
        if run_status == "COMPLETED":
            return counts
        time.sleep(10)
    raise TimeoutError(f"simulation run {run_id} exceeded timeout")


def main():
    args = parse_args()
    scripts_dir = Path(__file__).resolve().parent
    seed = load_module("simulation_seed", scripts_dir / "apply-simulation-seed.py")
    replay = load_module("replay_export", scripts_dir / "export-replay-fixture.py")

    manifest_bytes = args.manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    rows, wave_no = seed.load_manifest(args.manifest)
    if args.resume_run_id is not None:
        if args.auth_probe_only:
            raise ValueError(
                "--auth-probe-only cannot be combined with --resume-run-id"
            )
        run_id = args.resume_run_id
        print(f"resumeRunId={run_id}", flush=True)
    else:
        with connection() as database:
            with database.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM simulation_run "
                    "WHERE status = 'RUNNING'"
                )
                if cursor.fetchone()[0]:
                    raise RuntimeError("a simulation is already RUNNING")

        cookie = qa_cookie()
        verify_auth(args.base_url, cookie)
        if args.auth_probe_only:
            return
        counts = seed.apply(
            rows,
            wave_no,
            manifest_sha256,
            True,
            version_prefix=(
                args.seed_version_prefix or seed.VERSION_PREFIX
            ),
        )
        print(
            f"seedCommitted wave={wave_no} rows={len(rows)} counts={counts}",
            flush=True,
        )
        start_simulation(
            args.base_url,
            cookie,
            args.batch_size,
            args.battery_cell_count,
            args.capture_speed,
        )
        run_id = latest_running_run()
    terminal_counts = wait_for_run(run_id, args.timeout_seconds)

    fixture = replay.export_fixture([run_id], wave_no, manifest_sha256)
    fixture["terminalInspectionCounts"] = terminal_counts
    args.fixture_dir.mkdir(parents=True, exist_ok=True)
    fixture_path = args.fixture_dir / (
        f"wave-{wave_no:02d}-run-{run_id}.json"
    )
    fixture_path.write_text(
        json.dumps(
            fixture,
            ensure_ascii=False,
            indent=2,
            default=replay.json_default,
        ),
        encoding="utf-8",
    )
    fixture_sha256 = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
    sha_path = fixture_path.with_suffix(".json.sha256")
    sha_path.write_text(
        f"{fixture_sha256}  {fixture_path.name}\n",
        encoding="utf-8",
    )
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    key = f"{args.fixture_prefix.rstrip('/')}/{fixture_path.name}"
    s3.upload_file(str(fixture_path), "kt-aivle-big-proj-kks", key)
    s3.upload_file(
        str(sha_path),
        "kt-aivle-big-proj-kks",
        f"{key}.sha256",
    )
    print(
        f"fixtureUploaded=s3://kt-aivle-big-proj-kks/{key} "
        f"sha256={fixture_sha256}",
        flush=True,
    )


if __name__ == "__main__":
    main()
