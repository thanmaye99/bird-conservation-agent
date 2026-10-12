"""DynamoDB access layer for bird-call detections.

Table design (single table):
    PK  = "SITE#<site_id>"
    SK  = "TS#<iso8601_utc>#<species>"
    GSI1PK = "SPECIES#<species>"
    GSI1SK = "TS#<iso8601_utc>"

This lets us efficiently answer:
  * "what was detected at site X between t0 and t1"  (query PK + SK range)
  * "every detection of species Y over time"         (query GSI1)
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ.get("DETECTIONS_TABLE", "Detections")
GSI_NAME = "species-index"

_dynamodb = boto3.resource("dynamodb")


def _table():
    return _dynamodb.Table(TABLE_NAME)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_decimal(value: float) -> Decimal:
    # DynamoDB cannot store float; round-trip through str for precision safety.
    return Decimal(str(value))


def put_detection(
    *,
    site: str,
    species: str,
    scientific_name: str,
    confidence: float,
    source_key: str,
    detected_at: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a single detection and return the stored item."""
    detected_at = detected_at or _now_iso()
    item: dict[str, Any] = {
        "PK": f"SITE#{site}",
        "SK": f"TS#{detected_at}#{species}",
        "GSI1PK": f"SPECIES#{species}",
        "GSI1SK": f"TS#{detected_at}",
        "site": site,
        "species": species,
        "scientific_name": scientific_name,
        "confidence": _to_decimal(confidence),
        "detected_at": detected_at,
        "source_key": source_key,
    }
    if extra:
        item.update(extra)
    _table().put_item(Item=item)
    return item


def query_site(site: str, *, since: datetime | None = None, limit: int = 200) -> list[dict]:
    """Return detections at a site, newest first, optionally since a time."""
    cond = Key("PK").eq(f"SITE#{site}")
    if since is not None:
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        cond = cond & Key("SK").gte(f"TS#{since_iso}")
    resp = _table().query(
        KeyConditionExpression=cond,
        ScanIndexForward=False,
        Limit=limit,
    )
    return _decimals_to_float(resp.get("Items", []))


def query_species(species: str, *, since: datetime | None = None, limit: int = 500) -> list[dict]:
    """Return all detections for a species across sites, newest first."""
    cond = Key("GSI1PK").eq(f"SPECIES#{species}")
    if since is not None:
        since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        cond = cond & Key("GSI1SK").gte(f"TS#{since_iso}")
    resp = _table().query(
        IndexName=GSI_NAME,
        KeyConditionExpression=cond,
        ScanIndexForward=False,
        Limit=limit,
    )
    return _decimals_to_float(resp.get("Items", []))


def recent_detections(days: int = 30, limit: int = 2000) -> list[dict]:
    """Scan recent detections across all sites (used by the migration job).

    For large deployments replace this scan with a per-site fan-out query or a
    time-bucketed GSI; a scan is fine for modest data volumes.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
    items: list[dict] = []
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Key("detected_at").gte(cutoff_iso),
        "Limit": limit,
    }
    while True:
        resp = _table().scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp or len(items) >= limit:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return _decimals_to_float(items)


def species_last_heard(items: Iterable[dict]) -> dict[str, str]:
    """Map species -> most-recent detected_at from a list of detections."""
    last: dict[str, str] = {}
    for it in items:
        sp = it["species"]
        ts = it["detected_at"]
        if sp not in last or ts > last[sp]:
            last[sp] = ts
    return last


def _decimals_to_float(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        clean = {k: (float(v) if isinstance(v, Decimal) else v) for k, v in it.items()}
        out.append(clean)
    return out
