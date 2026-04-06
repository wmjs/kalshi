"""
Telegram command bot.

Polls getUpdates and responds to commands sent to the bot.

Supported commands:
    status  — runs scripts/status.py and returns the output
    restart — kills any running live_engine.py and starts a fresh one

Run standalone (alongside or separate from the live engine):
    python3 scripts/bot.py

Or as a systemd service — see DEPLOYMENT.md.
Only responds to messages from TELEGRAM_CHAT_ID for security.
"""

import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
from datetime import datetime, timezone
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
    "status": [sys.executable, "scripts/status.py", "--no-ws"],
}

HELP_TEXT = "Commands:\n  status — system status and today's setups\n  restart — restart the live engine"


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


def restart_engine() -> str:
    """
    Kill any running live_engine.py process, then start a fresh one detached
    from this process (start_new_session=True) so it outlives the bot.
    Returns a status string for the Telegram reply.
    """
    # Find and kill existing engine processes
    killed = []
    try:
        result = subprocess.run(
            ["pgrep", "-f", "live_engine.py"],
            capture_output=True, text=True,
        )
        pids = [int(p) for p in result.stdout.split() if p.strip()]
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
            except ProcessLookupError:
                pass
    except Exception as e:
        logger.warning("Error killing engine: %s", e)

    # Brief pause to let the old process clean up
    import time
    if killed:
        time.sleep(2)

    # Start fresh engine, detached from bot process
    log_date = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_path = ROOT / "logs" / f"run_{log_date}.log"
    log_path.parent.mkdir(exist_ok=True)

    python = sys.executable

    with open(log_path, "a") as log_file:
        log_file.write(f"\n[bot restart at {datetime.now(timezone.utc).isoformat()}]\n")
        subprocess.Popen(
            [python, "scripts/live_engine.py"],
            cwd=ROOT,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,   # detach from bot's process group
        )

    if killed:
        return f"Killed engine (pid {', '.join(str(p) for p in killed)}) and started fresh.\nLogging to {log_path.name}"
    return f"No running engine found. Started fresh.\nLogging to {log_path.name}"


async def handle(client: httpx.AsyncClient, msg: dict) -> None:
    chat_id = str(msg.get("chat", {}).get("id", ""))
    text    = msg.get("text", "").strip().lower()

    if chat_id != CHAT_ID:
        logger.warning("Ignoring message from unknown chat_id=%s", chat_id)
        return

    if text == "restart":
        logger.info("Restarting live engine")
        await send(client, "Restarting engine...")
        reply = await asyncio.get_event_loop().run_in_executor(None, restart_engine)
        await send(client, reply)
    elif text in COMMANDS:
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
