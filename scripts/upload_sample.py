#!/usr/bin/env python3
"""Upload a local audio clip to the ingest bucket using the key convention.

    <site>/<YYYY-MM-DD>/<filename>.wav

Optionally attaches lat/lon metadata so BirdNET can use location to refine
species probabilities.

Usage:
    python upload_sample.py path/to/birdcall.wav --site Site-1 \
        --bucket <AudioBucketName> [--lat 12.97 --lon 77.59] [--date 2026-06-15]

Find <AudioBucketName> in the CDK deploy outputs (or pass --stack to auto-resolve).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os

import boto3


def resolve_bucket_from_stack(stack_name: str) -> str:
    cfn = boto3.client("cloudformation")
    outputs = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["Outputs"]
    for o in outputs:
        if o["OutputKey"] == "AudioBucketName":
            return o["OutputValue"]
    raise SystemExit("AudioBucketName output not found on stack")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("audio", help="Path to a .wav file")
    p.add_argument("--site", required=True, help="Site id, e.g. Site-1")
    p.add_argument("--bucket", help="Ingest bucket name")
    p.add_argument("--stack", default="BirdConservationStack",
                   help="Stack to resolve the bucket from if --bucket omitted")
    p.add_argument("--date", help="Recording date YYYY-MM-DD (default: today)")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    args = p.parse_args()

    bucket = args.bucket or resolve_bucket_from_stack(args.stack)
    date_str = args.date or dt.date.today().isoformat()
    fname = os.path.basename(args.audio)
    key = f"{args.site}/{date_str}/{fname}"

    extra = {}
    metadata = {}
    if args.lat is not None and args.lon is not None:
        metadata = {"lat": str(args.lat), "lon": str(args.lon)}
        extra["Metadata"] = metadata

    boto3.client("s3").upload_file(args.audio, bucket, key, ExtraArgs=extra or None)
    print(f"Uploaded s3://{bucket}/{key}"
          + (f" with metadata {metadata}" if metadata else ""))


if __name__ == "__main__":
    main()
