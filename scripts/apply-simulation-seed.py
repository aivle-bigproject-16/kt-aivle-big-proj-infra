#!/usr/bin/env python3

import argparse
import csv
import hashlib
import os
from collections import Counter
from pathlib import Path

import psycopg


VERSION_PREFIX = "simulation-runtime-v17-wave"
BUCKET = "kt-aivle-big-proj-kks"
DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"
CELL_SERIALS = {f"SIM-{index:04d}" for index in range(1, 21)}
LEGACY_RECAPTURE_GROUPS = {
    ("SIM-0001", "CT"),
    ("SIM-0018", "CT"),
    ("SIM-0002", "RGB"),
    ("SIM-0016", "RGB"),
}
LEGACY_FIELDS = {
    "cell_serial_no",
    "image_type",
    "capture_set",
    "bucket_name",
    "object_key",
    "quality_label",
}
REQUIRED_FIELDS = {
    "wave_no",
    "logical_case_id",
    "cell_serial_no",
    "image_type",
    "capture_set",
    "bucket_name",
    "object_key",
    "quality_label",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument(
        "--version-prefix",
        default=VERSION_PREFIX,
        help="migration ledger prefix used for non-legacy wave manifests",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="commit the transaction; the default is a rollback-only dry run",
    )
    return parser.parse_args()


def load_manifest(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        fields = set(reader.fieldnames or ())
        legacy = fields == LEGACY_FIELDS
        if not legacy and not REQUIRED_FIELDS <= fields:
            raise ValueError(
                f"missing manifest fields: {sorted(REQUIRED_FIELDS - fields)}"
            )
        rows = list(reader)

    if legacy:
        if len(rows) != 880:
            raise ValueError(f"expected 880 legacy rows, got {len(rows)}")
        wave_no = "0"
    else:
        wave_numbers = {row["wave_no"] for row in rows}
        if len(wave_numbers) != 1:
            raise ValueError(f"manifest must contain one wave: {wave_numbers}")
        wave_no = next(iter(wave_numbers))
        if wave_no not in {"1", "2", "3", "4", "5"}:
            raise ValueError(f"unexpected wave_no: {wave_no}")

    group_counts = Counter()
    fail_counts = Counter()
    seen = set()
    for row in rows:
        cell = row["cell_serial_no"]
        image_type = row["image_type"]
        capture_set = row["capture_set"]
        quality_label = row["quality_label"]
        key = (cell, image_type, capture_set)

        if cell not in CELL_SERIALS:
            raise ValueError(f"unexpected cell: {cell}")
        if image_type not in {"CT", "RGB"}:
            raise ValueError(f"unexpected image type: {image_type}")
        if capture_set not in {"INITIAL", "RECAPTURE"}:
            raise ValueError(f"unexpected capture set: {capture_set}")
        if quality_label not in {"PASS", "FAIL"}:
            raise ValueError(f"unexpected quality label: {quality_label}")
        if row["bucket_name"] != BUCKET:
            raise ValueError(f"unexpected bucket: {row['bucket_name']}")
        if legacy:
            expected_path = f"/{capture_set.lower()}/{image_type.lower()}/"
        else:
            expected_path = (
                f"/initial_capture/{image_type}/images/"
                if capture_set == "INITIAL"
                else f"/recapture/{image_type}/images/"
            )
        if expected_path not in f"/{row['object_key']}":
            raise ValueError(f"capture path mismatch: {row['object_key']}")

        identity = (cell, row["bucket_name"], row["object_key"])
        if identity in seen:
            raise ValueError(f"duplicate source inside a cell: {identity}")
        seen.add(identity)
        group_counts[key] += 1
        fail_counts[key] += quality_label == "FAIL"

    for cell in sorted(CELL_SERIALS):
        for image_type in ("CT", "RGB"):
            initial = (cell, image_type, "INITIAL")
            expected_initial = 20 if legacy else 40
            if group_counts[initial] != expected_initial:
                raise ValueError(
                    f"{initial} must contain {expected_initial} images"
                )
            expected_fail_counts = (
                {1 if (cell, image_type) in LEGACY_RECAPTURE_GROUPS else 0}
                if legacy
                else {0, 9}
            )
            if fail_counts[initial] not in expected_fail_counts:
                raise ValueError(
                    f"{initial} has unexpected FAIL image count"
                )

            recapture = (cell, image_type, "RECAPTURE")
            expected_recaptures = (
                20 if legacy and (cell, image_type) in LEGACY_RECAPTURE_GROUPS
                else 40 if not legacy and fail_counts[initial] == 9
                else 0
            )
            if group_counts[recapture] != expected_recaptures:
                raise ValueError(
                    f"{recapture} must contain {expected_recaptures} images"
                )
            if fail_counts[recapture] != 0:
                raise ValueError(f"{recapture} must contain only PASS images")

    return rows, int(wave_no)


def connection_string():
    url = os.getenv("DB_URL", DEFAULT_DB_URL)
    if url.startswith("jdbc:"):
        url = url.removeprefix("jdbc:")
    return url


def apply(
    rows,
    wave_no,
    manifest_sha256,
    execute,
    version_prefix=VERSION_PREFIX,
):
    with psycopg.connect(
        connection_string(),
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("20260814_simulation_capture_sets",),
            )
            cursor.execute(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'battery_cell_image'
                      AND column_name = 'capture_set'
                )
                """
            )
            if not cursor.fetchone()[0]:
                raise RuntimeError("run the schema migration before the seed")

            cursor.execute(
                """
                CREATE TEMP TABLE simulation_seed_stage (
                    cell_serial_no varchar(100) NOT NULL,
                    image_type varchar(20) NOT NULL,
                    capture_set varchar(20) NOT NULL,
                    bucket_name varchar(100) NOT NULL,
                    object_key varchar(500) NOT NULL,
                    quality_label varchar(10) NOT NULL
                ) ON COMMIT DROP
                """
            )
            cursor.executemany(
                """
                INSERT INTO simulation_seed_stage VALUES (
                    %(cell_serial_no)s,
                    %(image_type)s,
                    %(capture_set)s,
                    %(bucket_name)s,
                    %(object_key)s,
                    %(quality_label)s
                )
                """,
                rows,
            )
            cursor.execute(
                """
                UPDATE public.battery_cell_image AS image
                SET capture_set = 'ARCHIVED'
                FROM public.battery_cell AS cell
                WHERE cell.id = image.battery_cell_id
                  AND cell.cell_serial_no LIKE 'SIM-%'
                """
            )
            cursor.execute(
                """
                INSERT INTO public.battery_cell_image (
                    battery_cell_id,
                    image_type,
                    capture_set,
                    bucket_name,
                    object_key,
                    storage_type,
                    file_name,
                    created_at
                )
                SELECT
                    cell.id,
                    stage.image_type,
                    stage.capture_set,
                    stage.bucket_name,
                    stage.object_key,
                    'S3',
                    regexp_replace(stage.object_key, '^.*/', ''),
                    now()
                FROM simulation_seed_stage AS stage
                JOIN public.battery_cell AS cell
                  ON cell.cell_serial_no = stage.cell_serial_no
                ON CONFLICT (battery_cell_id, bucket_name, object_key)
                DO UPDATE SET
                    image_type = EXCLUDED.image_type,
                    capture_set = EXCLUDED.capture_set,
                    storage_type = EXCLUDED.storage_type,
                    file_name = EXCLUDED.file_name
                """
            )
            cursor.execute(
                """
                INSERT INTO public.ops_simulation_seed_migration (
                    version,
                    manifest_sha256,
                    row_count,
                    applied_at
                ) VALUES (%s, %s, %s, now())
                ON CONFLICT (version) DO UPDATE SET
                    manifest_sha256 = EXCLUDED.manifest_sha256,
                    row_count = EXCLUDED.row_count,
                    applied_at = EXCLUDED.applied_at
                """,
                (
                    (
                        "simulation-runtime-v3"
                        if wave_no == 0
                        else f"{version_prefix}-{wave_no:02d}"
                    ),
                    manifest_sha256,
                    len(rows),
                ),
            )
            cursor.execute(
                """
                SELECT capture_set, image_type, count(*)
                FROM public.battery_cell_image AS image
                JOIN public.battery_cell AS cell
                  ON cell.id = image.battery_cell_id
                WHERE cell.cell_serial_no LIKE 'SIM-%'
                  AND capture_set IN ('INITIAL', 'RECAPTURE')
                GROUP BY capture_set, image_type
                ORDER BY capture_set, image_type
                """
            )
            counts = cursor.fetchall()
            expected_counts = Counter(
                (row["capture_set"], row["image_type"])
                for row in rows
            )
            persisted_counts = {
                (capture_set, image_type): count
                for capture_set, image_type, count in counts
            }
            if persisted_counts != dict(expected_counts):
                raise RuntimeError(f"unexpected persisted counts: {counts}")

        if execute:
            connection.commit()
        else:
            connection.rollback()
        return counts


def main():
    args = parse_args()
    manifest_bytes = args.manifest.read_bytes()
    rows, wave_no = load_manifest(args.manifest)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    counts = apply(
        rows,
        wave_no,
        digest,
        args.execute,
        version_prefix=args.version_prefix,
    )
    mode = "COMMITTED" if args.execute else "DRY RUN ROLLED BACK"
    print(f"{mode}: rows={len(rows)} sha256={digest} counts={counts}")


if __name__ == "__main__":
    main()
