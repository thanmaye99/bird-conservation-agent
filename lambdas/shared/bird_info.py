"""Lookups for bird metadata and imagery from free, public APIs.

  * GBIF        -> taxonomy + IUCN conservation status
  * Wikimedia   -> a representative image + short summary

All calls are best-effort: if a service is unavailable we degrade gracefully
rather than failing the whole alert.
"""
from __future__ import annotations

import functools
import requests

# In environments behind an SSL-intercepting corporate proxy, Python's default
# CA bundle rejects the proxy's self-signed cert. truststore makes Python use the
# OS trust store (e.g. macOS keychain), which contains the corporate root CA.
# Best-effort: harmless if truststore isn't installed (e.g. in Lambda).
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

_TIMEOUT = 8
_HEADERS = {"User-Agent": "BirdConservationAgent/1.0 (conservation project)"}


@functools.lru_cache(maxsize=512)
def get_bird_info(scientific_name: str) -> dict:
    """Return taxonomy + conservation status for a species (best-effort)."""
    info: dict = {
        "scientific_name": scientific_name,
        "conservation_status": "Unknown",
        "iucn_category": None,
        "taxonomy": {},
    }
    try:
        r = requests.get(
            "https://api.gbif.org/v1/species/match",
            params={"name": scientific_name},
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        match = r.json()
        info["taxonomy"] = {
            "family": match.get("family"),
            "order": match.get("order"),
            "genus": match.get("genus"),
        }
        usage_key = match.get("usageKey")
        if usage_key:
            iucn = requests.get(
                f"https://api.gbif.org/v1/species/{usage_key}/iucnRedListCategory",
                headers=_HEADERS,
                timeout=_TIMEOUT,
            )
            if iucn.ok:
                cat = iucn.json()
                info["iucn_category"] = cat.get("code")
                info["conservation_status"] = cat.get("category") or "Unknown"
    except requests.RequestException:
        pass
    return info


@functools.lru_cache(maxsize=512)
def get_bird_image(common_or_scientific_name: str) -> dict:
    """Return an image URL + short summary from Wikipedia (best-effort)."""
    result: dict = {"image_url": None, "summary": None, "page_url": None}
    try:
        title = common_or_scientific_name.replace(" ", "_")
        r = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}",
            headers=_HEADERS,
            timeout=_TIMEOUT,
        )
        if r.ok:
            data = r.json()
            result["summary"] = data.get("extract")
            result["page_url"] = (data.get("content_urls", {})
                                  .get("desktop", {})
                                  .get("page"))
            thumb = data.get("originalimage") or data.get("thumbnail")
            if thumb:
                result["image_url"] = thumb.get("source")
    except requests.RequestException:
        pass
    return result


# IUCN categories considered conservation-priority for real-time alerts.
PRIORITY_IUCN = {"CR", "EN", "VU", "NT"}  # Critically Endangered ... Near Threatened


def is_priority_species(scientific_name: str) -> bool:
    return get_bird_info(scientific_name).get("iucn_category") in PRIORITY_IUCN
