#!/usr/bin/env python3
"""Basic bird-call identifier.

Takes an audio file as input, runs it through the BirdNET classifier, and prints
the identified bird species (ranked by confidence).

Usage:
    python identify_bird.py path/to/birdcall.wav
    python identify_bird.py path/to/birdcall.mp3 --min-conf 0.25
    python identify_bird.py call.wav --lat 12.97 --lon 77.59 --date 2026-06-15

Location (--lat/--lon) and --date are optional but improve accuracy: BirdNET uses
them to weight species that are plausible for that place and time of year.

Install dependencies first:
    pip install -r requirements-basic.txt
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

# Reuse the shared bird_info module (Wikipedia photo/summary + GBIF/IUCN status).
_SHARED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lambdas", "shared")
sys.path.insert(0, _SHARED)


def identify(audio_path: str, *, min_conf: float,
             lat: float | None, lon: float | None,
             date_str: str | None) -> list[dict]:
    """Run BirdNET on an audio file and return detections (highest conf first)."""
    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer

    print("Loading BirdNET model (first run may download it, please wait)...")
    analyzer = Analyzer()

    kwargs: dict = {"min_conf": min_conf}
    if lat is not None and lon is not None:
        kwargs["lat"], kwargs["lon"] = lat, lon
    if date_str:
        kwargs["date"] = dt.datetime.strptime(date_str, "%Y-%m-%d")

    print(f"Analyzing '{os.path.basename(audio_path)}'...")
    recording = Recording(analyzer, audio_path, **kwargs)
    recording.analyze()

    return sorted(recording.detections,
                  key=lambda d: float(d["confidence"]), reverse=True)


def unique_species(detections: list[dict]) -> list[dict]:
    """Collapse per-chunk detections into one entry per species (best confidence)."""
    by_species: dict[str, dict] = {}
    for d in detections:
        key = d["scientific_name"]
        conf = float(d["confidence"])
        if key not in by_species or conf > by_species[key]["confidence"]:
            by_species[key] = {
                "common_name": d["common_name"],
                "scientific_name": d["scientific_name"],
                "confidence": conf,
                "best_time": f"{int(d.get('start_time', 0))}-{int(d.get('end_time', 0))}s",
            }
        by_species[key]["detections"] = by_species[key].get("detections", 0) + 1
    return sorted(by_species.values(), key=lambda s: s["confidence"], reverse=True)


# Wikimedia asks for a descriptive User-Agent and rate-limits aggressive clients.
_IMG_HEADERS = {
    "User-Agent": "BirdConservationAgent/1.0 (wildlife conservation project; "
                  "contact: conservation@example.org)"
}


def download_image(url: str, out_dir: str, name: str, *, retries: int = 3) -> str | None:
    """Download an image to out_dir; return the local path (best-effort).

    Retries with backoff on HTTP 429 (Wikimedia rate limiting).
    """
    import time
    import requests
    try:
        os.makedirs(out_dir, exist_ok=True)
        ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        safe = name.replace(" ", "_").replace("/", "_")
        path = os.path.join(out_dir, f"{safe}{ext}")
        for attempt in range(retries):
            resp = requests.get(url, timeout=20, headers=_IMG_HEADERS)
            if resp.status_code == 429:
                time.sleep(1.5 * (attempt + 1))  # back off and retry
                continue
            resp.raise_for_status()
            with open(path, "wb") as fh:
                fh.write(resp.content)
            return path
        return None
    except Exception:
        return None


def print_results(detections: list[dict], *, enrich: bool, save_dir: str | None) -> None:
    if not detections:
        print("\nNo bird identified above the confidence threshold.")
        print("Tip: try a lower --min-conf (e.g. 0.1) or a cleaner recording.")
        return

    species = unique_species(detections)
    print("\n" + "=" * 64)
    print(f"  IDENTIFIED {len(species)} SPECIES")
    print("=" * 64)

    if enrich:
        from bird_info import get_bird_info, get_bird_image

    for i, sp in enumerate(species, 1):
        print(f"\n{i}. {sp['common_name']}  ({sp['scientific_name']})")
        print(f"   Confidence:   {sp['confidence'] * 100:.1f}%  "
              f"(heard in {sp['detections']} segment(s), best at {sp['best_time']})")

        if not enrich:
            continue

        info = get_bird_info(sp["scientific_name"])
        image = get_bird_image(sp["common_name"])

        status = info.get("conservation_status") or "Unknown"
        cat = info.get("iucn_category")
        status_line = f"{status}" + (f" ({cat})" if cat else "")
        print(f"   Conservation: {status_line}")

        tax = info.get("taxonomy") or {}
        if tax.get("family"):
            print(f"   Family:       {tax.get('family')}")

        if image.get("summary"):
            summary = image["summary"]
            summary = summary if len(summary) <= 280 else summary[:277] + "..."
            print(f"   About:        {summary}")
        if image.get("image_url"):
            print(f"   Photo:        {image['image_url']}")
            if save_dir:
                local = download_image(image["image_url"], save_dir, sp["common_name"])
                if local:
                    print(f"   Saved photo:  {local}")
        if image.get("page_url"):
            print(f"   More info:    {image['page_url']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Identify a bird from an audio recording using BirdNET.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("audio", help="Path to an audio file (.wav, .mp3, .flac, ...)")
    parser.add_argument("--min-conf", type=float, default=0.25,
                        help="Minimum confidence to report (0-1, default 0.25)")
    parser.add_argument("--lat", type=float, help="Recording latitude (optional)")
    parser.add_argument("--lon", type=float, help="Recording longitude (optional)")
    parser.add_argument("--date", help="Recording date YYYY-MM-DD (optional)")
    parser.add_argument("--no-info", action="store_true",
                        help="Skip the photo + conservation-info lookup (faster)")
    parser.add_argument("--save-images", metavar="DIR", nargs="?",
                        const="bird_photos", default=None,
                        help="Download each species' photo into DIR (default: bird_photos)")
    args = parser.parse_args()

    if not os.path.exists(args.audio):
        sys.exit(f"Error: file not found: {args.audio}")

    detections = identify(
        args.audio,
        min_conf=args.min_conf,
        lat=args.lat,
        lon=args.lon,
        date_str=args.date,
    )
    print_results(detections, enrich=not args.no_info, save_dir=args.save_images)


if __name__ == "__main__":
    main()
