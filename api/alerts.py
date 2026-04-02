"""
Telegram alerting via Bot API.

Uses httpx (already a project dependency) — no additional packages needed.

Setup (one-time, ~2 minutes):
    1. Open Telegram, message @BotFather → /newbot → follow prompts → copy token
    2. Send any message to your new bot
    3. Fetch https://api.telegram.org/bot<TOKEN>/getUpdates → copy "id" from result.message.chat
    4. Add to .env:
           TELEGRAM_BOT_TOKEN=<token from BotFather>
           TELEGRAM_CHAT_ID=<your chat id>

If either credential is missing, send_alert() logs a debug message and returns silently.
Alerting must never crash the engine.
"""

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org"


async def send_alert(message: str) -> None:
    """
    Send a Telegram message. Silently no-ops if credentials are not configured.
    Never raises — alerting failures are logged as warnings only.
    """
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")

    if not all([token, chat_id]):
        logger.debug("Alert skipped: Telegram credentials not configured")
        return

    url = f"{_TELEGRAM_BASE}/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"chat_id": chat_id, "text": message})
        if resp.status_code != 200:
            logger.warning("Telegram alert failed: HTTP %d  %s", resp.status_code, resp.text[:200])
        else:
            logger.debug("Telegram alert sent: %s", message[:60])
    except Exception as e:
        logger.warning("Telegram alert error: %s", e)
