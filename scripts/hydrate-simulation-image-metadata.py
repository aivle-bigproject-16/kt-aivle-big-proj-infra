#!/usr/bin/env python3
"""Populate simulation image dimensions and CT slice metadata."""

import argparse
import csv
import os
import re
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
import psycopg


DEFAULT_DB_URL = (
    "postgresql://aws-0-ap-northeast-2.pooler.supabase.com:5432/postgres"
    "?sslmode=require"
)
DEFAULT_DB_USERNAME = "postgres.raybvdyfljopcfnhxiaq"
AXIS_VOLUMES = {"x": 100, "y": 254, "z": 871}
SLICE_PATTERN = re.compile(r"_(x|y|z)_(\d+)(?:__|\.|_)", re.IGNORECASE)
JPEG_SOF = {
    0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--workers", type=int, default=24)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="commit updates; the default rolls the transaction back",
    )
    return parser.parse_args()


def image_size(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("unsupported image format")
    offset = 2
    while offset + 9 <= len(data):
        if data[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9}:
            continue
        if offset + 2 > len(data):
            break
        length = struct.unpack(">H", data[offset:offset + 2])[0]
        if marker in JPEG_SOF and offset + 7 <= len(data):
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += length
    raise ValueError("image dimensions not found in header range")


def load_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {"cell_serial_no", "bucket_name", "object_key", "image_type"}
    if not rows or not required <= set(rows[0]):
        raise ValueError("manifest is missing simulation image fields")
    return rows


def metadata(row):
    match = SLICE_PATTERN.search(row["object_key"])
    axis = (row.get("axis") or (match.group(1) if match else "")).lower()
    return {
        "cell_serial_no": row["cell_serial_no"],
        "bucket_name": row["bucket_name"],
        "object_key": row["object_key"],
        "axis": axis or None,
        "slice_index": int(match.group(2)) if match else None,
        "volume": AXIS_VOLUMES.get(axis),
    }


def fetch_size(s3, identity):
    bucket, key = identity
    response = s3.get_object(Bucket=bucket, Key=key, Range="bytes=0-131071")
    return identity, image_size(response["Body"].read())


def connection_string():
    return os.getenv("DB_URL", DEFAULT_DB_URL).removeprefix("jdbc:")


def main():
    args = parse_args()
    rows = load_rows(args.manifest)
    enriched = [metadata(row) for row in rows]
    identities = sorted({
        (row["bucket_name"], row["object_key"])
        for row in enriched
    })
    s3 = boto3.client("s3", region_name="ap-northeast-2")
    sizes = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for identity, size in executor.map(
            lambda item: fetch_size(s3, item), identities
        ):
            sizes[identity] = size
    for row in enriched:
        row["width"], row["height"] = sizes[
            (row["bucket_name"], row["object_key"])
        ]

    with psycopg.connect(
        connection_string(),
        user=os.getenv("DB_USERNAME", DEFAULT_DB_USERNAME),
        password=os.environ["DB_PASSWORD"],
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("20260817_simulation_image_3d_metadata",),
            )
            cursor.executemany(
                """
                UPDATE public.battery_cell_image AS image
                SET width = %(width)s,
                    height = %(height)s,
                    axis = %(axis)s,
                    "index" = %(slice_index)s,
                    volume = %(volume)s
                FROM public.battery_cell AS cell
                WHERE cell.id = image.battery_cell_id
                  AND cell.cell_serial_no = %(cell_serial_no)s
                  AND image.bucket_name = %(bucket_name)s
                  AND image.object_key = %(object_key)s
                """,
                enriched,
            )
            cursor.execute(
                """
                UPDATE public.inspection_image AS target
                SET width = source.width,
                    height = source.height,
                    axis = source.axis,
                    "index" = source."index",
                    volume = source.volume
                FROM public.battery_cell_image AS source
                WHERE source.id = target.battery_cell_image_id
                """
            )
            propagated = cursor.rowcount
            if args.execute:
                connection.commit()
            else:
                connection.rollback()
    print(
        f"rows={len(enriched)} uniqueObjects={len(identities)} "
        f"propagatedInspectionImages={propagated} committed={args.execute}"
    )


if __name__ == "__main__":
    main()
