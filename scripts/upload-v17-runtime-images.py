#!/usr/bin/env python3
"""Upload only images referenced by v1.7 runtime wave manifests."""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError


PREFIX_MARKER = "simulations/server-simulation-v1.7/"
IMAGE_CONTENT_TYPE = "image/jpeg"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--region", default="ap-northeast-2")
    return parser.parse_args()


def load_objects(manifests, dataset_root):
    objects = {}
    for manifest in manifests:
        with manifest.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                key = row["object_key"]
                if not key.startswith(PREFIX_MARKER):
                    raise ValueError(f"unexpected object key: {key}")
                path = dataset_root / key.removeprefix(PREFIX_MARKER)
                if not path.is_file():
                    raise FileNotFoundError(path)
                identity = (row["bucket_name"], key)
                expected_hash = row["source_image_sha256"]
                previous = objects.setdefault(identity, (path, expected_hash))
                if previous != (path, expected_hash):
                    raise ValueError(f"conflicting object definition: {identity}")
    return objects


def main():
    args = parse_args()
    objects = load_objects(args.manifest, args.dataset_root)
    s3 = boto3.client("s3", region_name=args.region)
    transfer_config = TransferConfig(
        max_concurrency=1,
        multipart_threshold=64 * 1024 * 1024,
    )

    def upload(item):
        (bucket, key), (path, expected_hash) = item
        size = path.stat().st_size
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
            if (
                head["ContentLength"] == size
                and head.get("Metadata", {}).get("sha256") == expected_hash
                and head.get("ContentType") == IMAGE_CONTENT_TYPE
            ):
                return "skipped", size
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") not in {
                "404",
                "NoSuchKey",
                "NotFound",
            }:
                raise
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": IMAGE_CONTENT_TYPE,
                "Metadata": {"sha256": expected_hash},
            },
            Config=transfer_config,
        )
        return "uploaded", size

    counts = {"uploaded": 0, "skipped": 0}
    bytes_by_status = {"uploaded": 0, "skipped": 0}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(upload, item) for item in objects.items()]
        for index, future in enumerate(as_completed(futures), 1):
            status, size = future.result()
            counts[status] += 1
            bytes_by_status[status] += size
            if index % 250 == 0 or index == len(futures):
                print(
                    f"progress={index}/{len(futures)} "
                    f"uploaded={counts['uploaded']} skipped={counts['skipped']}"
                )

    print(
        f"complete objects={len(objects)} uploaded={counts['uploaded']} "
        f"skipped={counts['skipped']} "
        f"uploadedBytes={bytes_by_status['uploaded']}"
    )


if __name__ == "__main__":
    main()
