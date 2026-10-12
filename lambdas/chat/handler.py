"""Conversational API Lambda (Phase 4).

Backs an API Gateway POST /ask endpoint. Forest officials send a natural-language
question; Claude answers using read-only tools over the detection data. The
send_alert tool is intentionally NOT exposed here so the Q&A endpoint can never
spam officials.

Request  (JSON):  {"question": "What's been detected at Site-2 this week?"}
Response (JSON):  {"answer": "..."}
"""
from __future__ import annotations

import json

from bedrock_agent import run_agent

SYSTEM_PROMPT = (
    "You are a helpful wildlife-conservation assistant for forest officials. "
    "Answer questions about bird detections using the available tools:\n"
    "  * query_detections to look up what was heard at a site or for a species.\n"
    "  * get_bird_info for conservation status / taxonomy.\n"
    "  * get_bird_image for a photo and short description.\n"
    "Be concise and factual. When summarizing detections, mention counts, sites, "
    "dates and notable/threatened species. If you lack data, say so plainly rather "
    "than guessing. Today's interpretation of relative dates (e.g. 'this week') "
    "should map to the appropriate look-back in days when calling query_detections."
)

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,x-api-key",
    "Access-Control-Allow-Methods": "POST,OPTIONS",
    "Content-Type": "application/json",
}


def _response(status: int, body: dict) -> dict:
    return {"statusCode": status, "headers": _CORS, "body": json.dumps(body)}


def handler(event, context):
    # Support both REST API (proxy) and direct invocation.
    raw_body = event.get("body") if isinstance(event, dict) else None
    if raw_body is not None:
        try:
            payload = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        except json.JSONDecodeError:
            return _response(400, {"error": "Body must be valid JSON"})
    else:
        payload = event

    question = (payload or {}).get("question")
    if not question:
        return _response(400, {"error": "Missing 'question' field"})

    try:
        answer = run_agent(
            SYSTEM_PROMPT,
            question,
            allowed_tools=["query_detections", "get_bird_info", "get_bird_image"],
        )
    except Exception as exc:  # noqa: BLE001 - return a clean API error
        return _response(500, {"error": f"Agent error: {exc}"})

    return _response(200, {"answer": answer})
