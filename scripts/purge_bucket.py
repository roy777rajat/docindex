"""Delete every object version and delete marker in a bucket. Irreversible; requires --yes."""
import argparse

import boto3


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bucket")
    ap.add_argument("--region", default="eu-west-1")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    if not args.yes:
        raise SystemExit("refusing without --yes")
    s3 = boto3.client("s3", region_name=args.region)
    deleted = 0
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=args.bucket):
        batch = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        if batch:
            s3.delete_objects(Bucket=args.bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
    print(f"deleted {deleted} versions/markers from {args.bucket}")


if __name__ == "__main__":
    main()
