#!/usr/bin/env python3
"""Terminalize a known stale QA report without deleting evidence."""

import argparse
import os

import psycopg


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("report_type", choices=("individual", "daily"))
    parser.add_argument("report_id", type=int)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args()


def main():
    args = parse_args()
    if len(args.reason) > 255:
        raise ValueError("reason must be at most 255 characters")
    table = (
        "reports_individual"
        if args.report_type == "individual"
        else "reports_daily"
    )
    with psycopg.connect(
        os.getenv("DB_URL", DEFAULT_DB_URL).removeprefix("jdbc:"),
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT status, failure_reason FROM {table} "
                "WHERE id = %s FOR UPDATE",
                (args.report_id,),
            )
            before = cursor.fetchone()
            if before is None:
                raise RuntimeError("report does not exist")
            if before[0] != "PENDING":
                raise RuntimeError(f"report is not PENDING: {before[0]}")
            cursor.execute(
                f"UPDATE {table} SET status = 'FAILED', "
                "failure_reason = %s, updated_at = now() WHERE id = %s",
                (args.reason, args.report_id),
            )
        if args.execute:
            connection.commit()
        else:
            connection.rollback()
    mode = "COMMITTED" if args.execute else "DRY RUN ROLLED BACK"
    print(f"{mode}: table={table} reportId={args.report_id} before={before}")


if __name__ == "__main__":
    main()
