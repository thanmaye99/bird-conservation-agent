"""Scheduled migration-detection Lambda (daily EventBridge cron).

Detects two kinds of events from the detection history:

  * ARRIVAL  (migrated IN):  a species detected in the last `recent_days` that
    was NOT heard in the trailing window before that -> likely seasonal arrival.

  * DEPARTURE (migrated AWAY): a species that was regularly detected for weeks
    and then went silent for >= `DEPARTURE_SILENCE_DAYS` -> possible departure.

Because absence is not proof of migration (a recorder could be dead, or it's
just a quiet season), we run a GLOBAL SILENCE health check: if essentially every
species went quiet at the same time, we flag a likely sensor fault instead and
let the agent hedge accordingly.

The deterministic logic below finds *candidate* events; Claude then phrases them,
assigns a sensible confidence, and decides what to send to officials.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from detections import recent_detections
from bedrock_agent import run_agent

ANALYSIS_WINDOW_DAYS = int(os.environ.get("ANALYSIS_WINDOW_DAYS", "90"))
ARRIVAL_RECENT_DAYS = int(os.environ.get("ARRIVAL_RECENT_DAYS", "7"))
ARRIVAL_LOOKBACK_DAYS = int(os.environ.get("ARRIVAL_LOOKBACK_DAYS", "60"))
DEPARTURE_SILENCE_DAYS = int(os.environ.get("DEPARTURE_SILENCE_DAYS", "14"))
DEPARTURE_MIN_REGULAR_DAYS = int(os.environ.get("DEPARTURE_MIN_REGULAR_DAYS", "21"))

SYSTEM_PROMPT = (
    "You are a wildlife migration analyst supporting forest officials. You are "
    "given candidate migration events derived from acoustic-sensor data. For each "
    "event, write a short, plain-language note and assign a confidence (low / "
    "medium / high).\n"
    "Rules:\n"
    "  * ARRIVAL events are usually fairly reliable.\n"
    "  * DEPARTURE events are inferred from SILENCE and are inherently uncertain: "
    "a dead recorder, bad weather, or breeding-season quiet can mimic departure. "
    "Never state a departure as fact; say 'possible departure' and recommend "
    "verifying recorder health.\n"
    "  * If 'sensor_fault_suspected' is true, treat ALL departures as most likely a "
    "hardware/connectivity problem, not migration, and say so prominently.\n"
    "You may call get_bird_info for conservation status. Then call send_alert ONCE "
    "with a digest titled '[MIGRATION] Daily migration report' summarizing all "
    "events (group arrivals and departures). If there are no events, do not alert."
)


def _per_species_dates(items: list[dict]) -> dict[str, list[datetime]]:
    by_species: dict[str, list[datetime]] = defaultdict(list)
    for it in items:
        ts = datetime.strptime(it["detected_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc)
        by_species[it["species"]].append(ts)
    for sp in by_species:
        by_species[sp].sort()
    return by_species


def detect_events(items: list[dict], now: datetime) -> dict:
    by_species = _per_species_dates(items)
    recent_cut = now - timedelta(days=ARRIVAL_RECENT_DAYS)
    lookback_cut = now - timedelta(days=ARRIVAL_LOOKBACK_DAYS)
    silence_cut = now - timedelta(days=DEPARTURE_SILENCE_DAYS)
    regular_cut = now - timedelta(days=DEPARTURE_MIN_REGULAR_DAYS)

    arrivals, departures = [], []
    silent_species = 0

    for species, dates in by_species.items():
        last = dates[-1]
        heard_recently = last >= recent_cut
        heard_before = any(lookback_cut <= d < recent_cut for d in dates)

        # Arrival: present now, absent in the prior window.
        if heard_recently and not heard_before:
            arrivals.append({
                "species": species,
                "first_seen_this_window": min(d for d in dates if d >= recent_cut)
                .strftime("%Y-%m-%d"),
                "detections_recent": sum(1 for d in dates if d >= recent_cut),
            })

        # Departure: was regular for weeks, now silent.
        was_regular = sum(1 for d in dates if regular_cut <= d <= silence_cut) >= 5
        if was_regular and last < silence_cut:
            silent_species += 1
            departures.append({
                "species": species,
                "last_heard": last.strftime("%Y-%m-%d"),
                "days_silent": (now - last).days,
                "prior_detections": len(dates),
            })

    # Global-silence health check: if a large share of previously-active species
    # all went silent, suspect a sensor fault rather than mass migration.
    active_species = len(by_species)
    sensor_fault_suspected = bool(
        active_species >= 3 and silent_species / max(active_species, 1) >= 0.8
    )

    return {
        "arrivals": arrivals,
        "departures": departures,
        "sensor_fault_suspected": sensor_fault_suspected,
        "active_species": active_species,
    }


def handler(event, context):
    now = datetime.now(timezone.utc)
    items = recent_detections(days=ANALYSIS_WINDOW_DAYS)
    events = detect_events(items, now)

    if not events["arrivals"] and not events["departures"]:
        return {"status": "no_events", **events}

    prompt = (
        "Candidate migration events derived from acoustic data:\n"
        f"{json.dumps(events, indent=2)}\n\n"
        "Write the daily migration report and send it to officials."
    )
    summary = run_agent(
        SYSTEM_PROMPT,
        prompt,
        allowed_tools=["get_bird_info", "send_alert"],
    )
    return {"status": "reported", "events": events, "agent_summary": summary}
