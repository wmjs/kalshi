"""
SMS alerting via Twilio REST API.

Uses httpx (already a project dependency) — no additional packages needed.

Environment variables (.env):
    TWILIO_ACCOUNT_SID   — Twilio account SID (starts with AC)
    TWILIO_AUTH_TOKEN    — Twilio auth token
    TWILIO_FROM          — Twilio phone number (E.164 format, e.g. +12025551234)
    TWILIO_TO            — Your phone number (E.164 format)

If any credential is missing, send_sms() logs a warning and returns silently.
Alerting must never crash the engine.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TWILIO_BASE = "https://api.twilio.com/2010-04-01/Accounts"


async def send_sms(message: str) -> None:
    """
    Send an SMS via Twilio. Silently no-ops if credentials are not configured.
    Never raises — alerting failures are logged as warnings only.
    """
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM")
    to    = os.getenv("TWILIO_TO")

    if not all([sid, token, from_, to]):
        logger.debug("SMS skipped: Twilio credentials not configured")
        return

    url = f"{_TWILIO_BASE}/{sid}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                auth=(sid, token),
                data={"From": from_, "To": to, "Body": message},
            )
        if resp.status_code not in (200, 201):
            logger.warning("SMS failed: HTTP %d  %s", resp.status_code, resp.text[:200])
        else:
            logger.debug("SMS sent: %s", message[:60])
    except Exception as e:
        logger.warning("SMS error: %s", e)
