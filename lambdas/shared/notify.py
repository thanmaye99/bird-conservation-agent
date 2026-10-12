"""Alert delivery via Amazon SNS."""
from __future__ import annotations

import os

# DRY_RUN lets the local test harness exercise the full agent without sending
# real notifications (and without needing SNS configured).
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOPIC_ARN = os.environ.get("ALERT_TOPIC_ARN", "")

if not DRY_RUN:
    import boto3
    _sns = boto3.client("sns")


def send_alert(subject: str, message: str, *, image_url: str | None = None) -> dict:
    """Publish an alert to the SNS topic (email + SMS subscribers).

    SMS ignores subject and truncates; email shows both. We append the image
    link to the body since SNS email is plain text.
    """
    if image_url:
        message = f"{message}\n\nPhoto: {image_url}"
    # SNS subject max length is 100 chars and disallows newlines.
    safe_subject = subject.replace("\n", " ").strip()[:100]

    if DRY_RUN:
        print("\n===== [DRY RUN] ALERT (not actually sent) =====")
        print(f"Subject: {safe_subject}")
        print(f"Message:\n{message}")
        print("===============================================\n")
        return {"dry_run": True, "subject": safe_subject}

    return _sns.publish(
        TopicArn=TOPIC_ARN,
        Subject=safe_subject,
        Message=message,
    )
