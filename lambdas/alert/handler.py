"""Real-time alert Lambda.

Invoked asynchronously by the inference Lambda when a notable detection occurs.
It hands the detection to Claude, which uses tools to fetch the bird's
conservation status + photo, decides how urgent the alert is, and sends a
well-written message to forest officials via SNS.
"""
from __future__ import annotations

from bedrock_agent import run_agent

SYSTEM_PROMPT = (
    "You are a wildlife conservation assistant supporting forest officials. "
    "When a bird is detected by an acoustic sensor, you research the species and "
    "send a concise, useful alert. Always:\n"
    "  1. Call get_bird_info (with the scientific name) to learn its IUCN status.\n"
    "  2. Call get_bird_image (with the common name) to fetch a photo and summary.\n"
    "  3. Call send_alert with a clear subject and message. Prefix the subject with "
    "an urgency marker: '[PRIORITY]' for Near Threatened or worse (NT/VU/EN/CR), "
    "otherwise '[INFO]'. Include the photo URL.\n"
    "Keep the message factual: species (common + scientific), site, time, detection "
    "confidence, conservation status, and one line of natural-history context. "
    "Do not invent facts; rely on tool output."
)


def handler(event, context):
    species = event["species"]
    payload = (
        f"A bird call was detected.\n"
        f"- Common name: {species}\n"
        f"- Scientific name: {event['scientific_name']}\n"
        f"- Site: {event['site']}\n"
        f"- Time (UTC): {event['detected_at']}\n"
        f"- Detection confidence: {event['confidence']:.2f}\n\n"
        f"Research this species and send an appropriate alert to officials."
    )
    answer = run_agent(
        SYSTEM_PROMPT,
        payload,
        allowed_tools=["get_bird_info", "get_bird_image", "send_alert"],
    )
    return {"status": "ok", "agent_summary": answer}
