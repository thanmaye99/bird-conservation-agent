"""BirdNET inference Lambda.

Triggered by S3 ObjectCreated events on the raw-audio bucket. For each uploaded
audio file it:
  1. downloads the clip to /tmp
  2. runs BirdNET (via birdnetlib) to identify species
  3. writes each above-threshold detection to DynamoDB
  4. asynchronously invokes the alert Lambda for notable detections

Site id and (optional) lat/lon/date are taken from the S3 object key prefix or
object metadata. Convention for the key:
    <site>/<YYYY-MM-DD>/<filename>.wav
e.g.  Site-1/2026-06-15/dawn-0612.wav
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from datetime import datetime, timezone

import boto3

from birdnetlib import Recording
from birdnetlib.analyzer import Analyzer

from detections import put_detection

MIN_CONFIDENCE = float(os.environ.get("MIN_CONFIDENCE", "0.5"))
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "0.7"))
ALERT_FUNCTION = os.environ.get("ALERT_FUNCTION_NAME", "")

_s3 = boto3.client("s3")
_lambda = boto3.client("lambda")

# Analyzer load is expensive (~loads the TFLite model); keep it module-global so
# it is reused across warm invocations.
_analyzer = Analyzer()

_KEY_RE = re.compile(r"^(?P<site>[^/]+)/(?P<date>\d{4}-\d{2}-\d{2})/")


def _parse_key(key: str) -> tuple[str, str | None]:
    """Extract (site, date) from the object key using our naming convention."""
    m = _KEY_RE.match(key)
    if m:
        return m.group("site"), m.group("date")
    # Fallback: first path segment is the site, no date.
    site = key.split("/")[0] if "/" in key else "unknown"
    return site, None


def _analyze(path: str, lat: float | None, lon: float | None, date_str: str | None):
    kwargs = {"min_conf": MIN_CONFIDENCE}
    if lat is not None and lon is not None:
        kwargs["lat"] = lat
        kwargs["lon"] = lon
    if date_str:
        kwargs["date"] = datetime.strptime(date_str, "%Y-%m-%d")
    recording = Recording(_analyzer, path, **kwargs)
    recording.analyze()
    return recording.detections


def handler(event, context):
    results = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        site, date_str = _parse_key(key)

        local = f"/tmp/{os.path.basename(key)}"
        _s3.download_file(bucket, key, local)

        head = _s3.head_object(Bucket=bucket, Key=key).get("Metadata", {})
        lat = float(head["lat"]) if "lat" in head else None
        lon = float(head["lon"]) if "lon" in head else None

        detections = _analyze(local, lat, lon, date_str)
        detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for det in detections:
            common = det["common_name"]
            scientific = det["scientific_name"]
            conf = float(det["confidence"])
            put_detection(
                site=site,
                species=common,
                scientific_name=scientific,
                confidence=conf,
                source_key=key,
                detected_at=detected_at,
                extra={"start_time": int(det.get("start_time", 0)),
                       "end_time": int(det.get("end_time", 0))},
            )
            results.append({"species": common, "confidence": conf, "site": site})

            if conf >= ALERT_THRESHOLD and ALERT_FUNCTION:
                _lambda.invoke(
                    FunctionName=ALERT_FUNCTION,
                    InvocationType="Event",  # async, fire-and-forget
                    Payload=json.dumps({
                        "type": "detection",
                        "site": site,
                        "species": common,
                        "scientific_name": scientific,
                        "confidence": conf,
                        "detected_at": detected_at,
                    }).encode(),
                )

        try:
            os.remove(local)
        except OSError:
            pass

    return {"detections": results, "count": len(results)}
