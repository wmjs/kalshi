"""
Telegram command bot.

Polls getUpdates and responds to commands sent to the bot.

Supported commands:
    status  — runs scripts/status.py and returns the output

Run standalone (alongside or separate from the live engine):
    python3 scripts/bot.py

Or as a systemd service — see DEPLOYMENT.md.
Only responds to messages from TELEGRAM_CHAT_ID for security.
"""

import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s")
logger = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

COMMANDS = {
    "status": ["python3", "scripts/status.py", "--no-ws"],
}

HELP_TEXT = "Commands:\n  status — system status and today's setups"


async def get_updates(client: httpx.AsyncClient, offset: int) -> list[dict]:
    resp = await client.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"offset": offset, "timeout": 30},
        timeout=35.0,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


async def send(client: httpx.AsyncClient, text: str) -> None:
    """Send plain text, splitting if needed (Telegram max 4096 chars)."""
    for chunk in [text[i:i + 4000] for i in range(0, len(text), 4000)]:
        await client.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": chunk},
            timeout=10.0,
        )


def run_command(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    output = result.stdout
    if result.returncode != 0:
        output += result.stderr
    return ANSI_RE.sub("", output).strip()


async def handle(client: httpx.AsyncClient, msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip().lower()

    if chat_id != CHAT_ID:
        logger.warning("Ignoring message from unknown chat_id=%s", chat_id)
        return

    if text in COMMANDS:
        logger.info("Running command: %s", text)
        await send(client, f"Running {text}...")
        output = run_command(COMMANDS[text])
        await send(client, output or "(no output)")
    else:
        await send(client, HELP_TEXT)


async def main() -> None:
    if not TOKEN or not CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")
        sys.exit(1)

    logger.info("Bot started — polling for commands")
    offset = 0

    async with httpx.AsyncClient() as client:
        while True:
            try:
                updates = await get_updates(client, offset)
                for update in updates:
                    offset = update["update_id"] + 1
                    msg = update.get("message", {})
                    if msg:
                        await handle(client, msg)
            except httpx.HTTPError as e:
                logger.warning("HTTP error: %s — retrying in 5s", e)
                await asyncio.sleep(5)
            except Exception as e:
                logger.error("Unexpected error: %s — retrying in 5s", e)
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
