#!/usr/bin/env python3
"""Local test harness — run BirdNET (and optionally the agent) on a sample file.

This lets you validate the pipeline on your laptop before any AWS deploy.

Modes:
  * default        : run BirdNET on the file and print detected species.
  * --alert        : also run the Claude alert agent on the top detection in
                     DRY-RUN mode (composes a real alert but prints it instead
                     of sending SNS). Requires AWS creds + Bedrock model access.
  * --ask "..."    : run the conversational agent against a question. Requires a
                     deployed DynamoDB table (set DETECTIONS_TABLE) + Bedrock.

Examples:
  python scripts/local_test.py sample.wav
  python scripts/local_test.py sample.wav --lat 12.97 --lon 77.59 --alert
  DETECTIONS_TABLE=DetectionsTable-XYZ \
      python scripts/local_test.py sample.wav --ask "What was heard at Site-1?"

Install local deps first:
  pip install -r requirements-dev.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

# DRY_RUN must be set BEFORE importing any shared module that reads it.
os.environ.setdefault("DRY_RUN", "1")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_DIR = os.path.join(REPO_ROOT, "lambdas", "shared")
sys.path.insert(0, SHARED_DIR)


def analyze_file(path: str, lat: float | None, lon: float | None,
                 date_str: str | None, min_conf: float) -> list[dict]:
    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer

    print("Loading BirdNET model (first run downloads it)...")
    analyzer = Analyzer()
    kwargs = {"min_conf": min_conf}
    if lat is not None and lon is not None:
        kwargs["lat"], kwargs["lon"] = lat, lon
    if date_str:
        kwargs["date"] = dt.datetime.strptime(date_str, "%Y-%m-%d")

    recording = Recording(analyzer, path, **kwargs)
    recording.analyze()
    return recording.detections


def print_detections(detections: list[dict]) -> None:
    if not detections:
        print("\nNo species detected above the confidence threshold.")
        return
    print(f"\nDetected {len(detections)} call(s):")
    print(f"  {'Common name':<28} {'Scientific name':<30} {'Conf':>5}  {'Time'}")
    print("  " + "-" * 78)
    for d in detections:
        span = f"{int(d.get('start_time', 0))}-{int(d.get('end_time', 0))}s"
        print(f"  {d['common_name']:<28} {d['scientific_name']:<30} "
              f"{float(d['confidence']):>5.2f}  {span}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("audio", help="Path to a .wav file")
    p.add_argument("--site", default="Site-1")
    p.add_argument("--lat", type=float)
    p.add_argument("--lon", type=float)
    p.add_argument("--date", help="Recording date YYYY-MM-DD (default: today)")
    p.add_argument("--min-conf", type=float, default=0.25,
                   help="BirdNET min confidence (default 0.25 for testing)")
    p.add_argument("--alert", action="store_true",
                   help="Run the Claude alert agent (DRY RUN) on the top detection")
    p.add_argument("--ask", metavar="QUESTION",
                   help="Run the conversational agent with a question")
    args = p.parse_args()

    if not os.path.exists(args.audio):
        raise SystemExit(f"File not found: {args.audio}")

    detections = analyze_file(args.audio, args.lat, args.lon,
                              args.date, args.min_conf)
    print_detections(detections)

    if args.alert:
        if not detections:
            print("\n[--alert] Skipped: no detections to alert on.")
        else:
            top = max(detections, key=lambda d: float(d["confidence"]))
            print(f"\n[--alert] Running Claude alert agent for '{top['common_name']}' "
                  f"(DRY RUN — no SNS will be sent)...\n")
            from bedrock_agent import run_agent
            system = (
                "You are a wildlife conservation assistant. Research the detected "
                "species (call get_bird_info with the scientific name and "
                "get_bird_image with the common name), then call send_alert with a "
                "clear subject (prefix '[PRIORITY]' if IUCN NT/VU/EN/CR else "
                "'[INFO]') and an informative message including the photo URL."
            )
            prompt = (
                f"A bird call was detected.\n"
                f"- Common name: {top['common_name']}\n"
                f"- Scientific name: {top['scientific_name']}\n"
                f"- Site: {args.site}\n"
                f"- Time (UTC): {dt.datetime.utcnow():%Y-%m-%dT%H:%M:%SZ}\n"
                f"- Detection confidence: {float(top['confidence']):.2f}\n\n"
                f"Research this species and send an appropriate alert."
            )
            answer = run_agent(
                system, prompt,
                allowed_tools=["get_bird_info", "get_bird_image", "send_alert"],
            )
            print(f"Agent summary:\n{answer}")

    if args.ask:
        if not os.environ.get("DETECTIONS_TABLE"):
            print("\n[--ask] Set DETECTIONS_TABLE to a deployed table to query data.")
        print(f"\n[--ask] Question: {args.ask}\n")
        from bedrock_agent import run_agent
        system = (
            "You are a wildlife-conservation assistant. Use query_detections, "
            "get_bird_info and get_bird_image to answer questions about bird "
            "detections. Be concise and factual; say so if you lack data."
        )
        answer = run_agent(
            system, args.ask,
            allowed_tools=["query_detections", "get_bird_info", "get_bird_image"],
        )
        print(f"Answer:\n{answer}")


if __name__ == "__main__":
    main()
