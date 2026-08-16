#!/usr/bin/env python3
"""Safely terminalize a stale simulation without deleting its history."""

import argparse
import os

import psycopg


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"
ACTIVE_STATUSES = ("PENDING", "CAPTURING", "CAPTURED", "ANALYZING")


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id", type=int)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--reason",
        default="QA_PREP_STALE_RUN_RECOVERY_20260816",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    if len(args.reason) > 100:
        raise ValueError("reason must be at most 100 characters")
    url = os.getenv("DB_URL", DEFAULT_DB_URL).removeprefix("jdbc:")
    with psycopg.connect(
        url,
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (f"close_stale_simulation_{args.run_id}",),
            )
            cursor.execute(
                """
                SELECT status, started_at
                FROM simulation_run
                WHERE id = %s
                FOR UPDATE
                """,
                (args.run_id,),
            )
            run = cursor.fetchone()
            if run is None:
                raise RuntimeError("simulation run does not exist")
            if run[0] != "RUNNING":
                raise RuntimeError(f"simulation run is not RUNNING: {run[0]}")

            cursor.execute(
                """
                SELECT i.status, count(*)
                FROM inspection i
                JOIN inspection_batch b ON b.id = i.inspection_batch_id
                WHERE b.simulation_run_id = %s
                GROUP BY i.status
                ORDER BY i.status
                """,
                (args.run_id,),
            )
            before = cursor.fetchall()

            cursor.execute(
                """
                UPDATE inspection i
                SET status = 'FAILED',
                    final_label = 'FAIL',
                    failure_type = 'AI',
                    failure_reason = %s,
                    analyzed_at = now(),
                    updated_at = now()
                FROM inspection_batch b
                WHERE b.id = i.inspection_batch_id
                  AND b.simulation_run_id = %s
                  AND i.status = ANY(%s)
                """,
                (args.reason, args.run_id, list(ACTIVE_STATUSES)),
            )
            inspections_updated = cursor.rowcount

            cursor.execute(
                """
                UPDATE inspection_batch
                SET status = 'COMPLETED', updated_at = now()
                WHERE simulation_run_id = %s
                """,
                (args.run_id,),
            )
            batches_updated = cursor.rowcount
            cursor.execute(
                """
                UPDATE simulation_run
                SET status = 'COMPLETED', ended_at = now()
                WHERE id = %s AND status = 'RUNNING'
                """,
                (args.run_id,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("simulation run was not terminalized")

        if args.execute:
            connection.commit()
        else:
            connection.rollback()

    mode = "COMMITTED" if args.execute else "DRY RUN ROLLED BACK"
    print(
        f"{mode}: runId={args.run_id} before={before} "
        f"inspectionsUpdated={inspections_updated} "
        f"batchesUpdated={batches_updated}"
    )


if __name__ == "__main__":
    main()
