#!/usr/bin/env bash
# Daily orchestration: refresh data → run live engine
# Restarts the engine up to MAX_ATTEMPTS times on non-zero exit.
# Sends SMS alert if all attempts fail.
#
# Usage:
#   bash scripts/run_daily.sh
#
# Cron example (07:00 UTC daily):
#   0 7 * * * cd /path/to/kalshi && bash scripts/run_daily.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

MAX_ATTEMPTS=3
LOGDIR="logs"
DATE=$(date -u +%Y%m%d)
RUNLOG="$LOGDIR/run_$DATE.log"

mkdir -p "$LOGDIR"

log() {
    echo "[$(date -u '+%H:%M:%S')] $*" | tee -a "$RUNLOG"
}

log "=== Daily run started ($(date -u)) ==="

# ---------------------------------------------------------------------------
# 1. Refresh market data
# ---------------------------------------------------------------------------
log "Step 1: daily_refresh.py"
if python3 scripts/daily_refresh.py >> "$RUNLOG" 2>&1; then
    log "Refresh complete."
else
    log "WARNING: refresh failed (exit $?). Continuing with existing data."
fi

# ---------------------------------------------------------------------------
# 2. Run live engine with restart on crash
# ---------------------------------------------------------------------------
log "Step 2: live_engine.py (max $MAX_ATTEMPTS attempts)"

attempt=0
while [ $attempt -lt $MAX_ATTEMPTS ]; do
    attempt=$((attempt + 1))
    log "Engine attempt $attempt/$MAX_ATTEMPTS..."

    python3 scripts/live_engine.py >> "$RUNLOG" 2>&1
    exit_code=$?

    if [ $exit_code -eq 0 ]; then
        log "Engine exited cleanly."
        log "=== Daily run complete ==="
        exit 0
    fi

    log "Engine exited with code $exit_code."

    if [ $attempt -lt $MAX_ATTEMPTS ]; then
        log "Waiting 30s before restart..."
        sleep 30
    fi
done

# ---------------------------------------------------------------------------
# 3. All attempts failed — send SMS alert
# ---------------------------------------------------------------------------
log "ERROR: engine failed after $MAX_ATTEMPTS attempts. Sending alert."

python3 - <<'PYEOF' >> "$RUNLOG" 2>&1
import asyncio, sys
sys.path.insert(0, '.')
from api.alerts import send_sms
asyncio.run(send_sms(f"Kalshi engine crashed after 3 attempts. Manual check required."))
PYEOF

log "=== Daily run FAILED ==="
exit 1
