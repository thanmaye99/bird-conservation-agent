"""The agent 'brain': Claude on Amazon Bedrock with tool calling.

Implemented with the Bedrock Converse API, which gives a model-agnostic
tool-use interface. The agent is given a set of Python-backed tools and runs an
autonomous loop until it produces a final natural-language answer.

This single module powers both:
  * the real-time alert Lambda (compose & send a rich species alert)
  * the migration Lambda (reason about arrivals/departures, then alert)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

import boto3

from detections import query_site, query_species
from bird_info import get_bird_info, get_bird_image
from notify import send_alert as _send_alert

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-5-sonnet-20240620-v1:0")
MAX_TOOL_TURNS = 8

_bedrock = boto3.client("bedrock-runtime")


# --------------------------------------------------------------------------- #
# Tool implementations (the agent's "action group")                           #
# --------------------------------------------------------------------------- #
def _tool_query_detections(site: str | None = None, species: str | None = None,
                           days: int = 30) -> dict:
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(days=days)
    if species:
        items = query_species(species, since=since)
    elif site:
        items = query_site(site, since=since)
    else:
        return {"error": "provide either 'site' or 'species'"}
    slim = [
        {"species": i["species"], "site": i["site"],
         "detected_at": i["detected_at"], "confidence": i.get("confidence")}
        for i in items
    ]
    return {"count": len(slim), "detections": slim[:50]}


def _tool_get_bird_info(scientific_name: str) -> dict:
    return get_bird_info(scientific_name)


def _tool_get_bird_image(name: str) -> dict:
    return get_bird_image(name)


def _tool_send_alert(subject: str, message: str, image_url: str | None = None) -> dict:
    _send_alert(subject, message, image_url=image_url)
    return {"status": "sent", "subject": subject}


TOOL_IMPL: dict[str, Callable[..., dict]] = {
    "query_detections": _tool_query_detections,
    "get_bird_info": _tool_get_bird_info,
    "get_bird_image": _tool_get_bird_image,
    "send_alert": _tool_send_alert,
}

TOOL_SPEC = [
    {
        "toolSpec": {
            "name": "query_detections",
            "description": "Query stored bird-call detections by site or by species "
                           "over the last N days. Returns matching detection records.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "Site id, e.g. 'Site-1'"},
                    "species": {"type": "string", "description": "Common species name"},
                    "days": {"type": "integer", "description": "Look-back window in days"},
                },
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_bird_info",
            "description": "Get taxonomy and IUCN conservation status for a species "
                           "by its scientific (Latin) name.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"scientific_name": {"type": "string"}},
                "required": ["scientific_name"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "get_bird_image",
            "description": "Get a representative photo URL and a short summary for a "
                           "bird by common or scientific name.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }},
        }
    },
    {
        "toolSpec": {
            "name": "send_alert",
            "description": "Send an alert to forest officials via email/SMS. Use a short "
                           "subject and an informative message. Include image_url when known.",
            "inputSchema": {"json": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "message": {"type": "string"},
                    "image_url": {"type": "string"},
                },
                "required": ["subject", "message"],
            }},
        }
    },
]


def run_agent(system_prompt: str, user_prompt: str,
              *, allowed_tools: list[str] | None = None) -> str:
    """Run Claude with the tool loop until it returns a final text answer."""
    tool_config = {"tools": TOOL_SPEC}
    if allowed_tools is not None:
        tool_config = {"tools": [t for t in TOOL_SPEC
                                 if t["toolSpec"]["name"] in allowed_tools]}

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"text": user_prompt}]}
    ]

    for _ in range(MAX_TOOL_TURNS):
        resp = _bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": system_prompt}],
            messages=messages,
            toolConfig=tool_config,
            inferenceConfig={"maxTokens": 1500, "temperature": 0.2},
        )
        out = resp["output"]["message"]
        messages.append(out)

        if resp.get("stopReason") != "tool_use":
            return "".join(b.get("text", "") for b in out["content"])

        tool_results = []
        for block in out["content"]:
            if "toolUse" not in block:
                continue
            tu = block["toolUse"]
            fn = TOOL_IMPL.get(tu["name"])
            try:
                result = fn(**tu["input"]) if fn else {"error": f"unknown tool {tu['name']}"}
            except Exception as exc:  # surface tool errors back to the model
                result = {"error": str(exc)}
            tool_results.append({
                "toolResult": {
                    "toolUseId": tu["toolUseId"],
                    "content": [{"json": result}],
                }
            })
        messages.append({"role": "user", "content": tool_results})

    return "Agent stopped after reaching the maximum number of tool turns."
