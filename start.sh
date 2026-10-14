#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

activate_venv() {
  if [[ ! -d .venv ]]; then
    python3 -m venv .venv
    # shellcheck disable=SC1091
    source .venv/bin/activate
    pip install -U pip
    pip install -r requirements.txt
  else
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
}

export VERO_HOST="${VERO_HOST:-127.0.0.1}"
export VERO_PORT="${VERO_PORT:-8900}"

cmd="${1:-server}"

activate_venv

case "$cmd" in
  server|"")
    echo "VERO → http://${VERO_HOST}:${VERO_PORT}"
    exec python server.py
    ;;
  guard|supervisor)
    export VERO_SUPERVISOR_HOST="${VERO_SUPERVISOR_HOST:-$VERO_HOST}"
    export VERO_SUPERVISOR_PORT="${VERO_SUPERVISOR_PORT:-8901}"
    echo "VERO supervisor → http://${VERO_SUPERVISOR_HOST}:${VERO_SUPERVISOR_PORT}/"
    exec python -m agents.supervisor
    ;;
  open|launch)
    export VERO_SUPERVISOR_HOST="${VERO_SUPERVISOR_HOST:-$VERO_HOST}"
    export VERO_SUPERVISOR_PORT="${VERO_SUPERVISOR_PORT:-8901}"
    # поднять guard в фоне, если ещё не слушает
    if ! curl -sf --connect-timeout 1 "http://${VERO_SUPERVISOR_HOST}:${VERO_SUPERVISOR_PORT}/api/status" >/dev/null; then
      echo "starting supervisor…"
      nohup python -m agents.supervisor >> data/cache/vero_supervisor.log 2>&1 &
      for i in 1 2 3 4 5 6 7 8 9 10; do
        sleep 0.5
        curl -sf --connect-timeout 1 "http://${VERO_SUPERVISOR_HOST}:${VERO_SUPERVISOR_PORT}/api/status" >/dev/null && break
      done
    fi
    url="http://${VERO_SUPERVISOR_HOST}:${VERO_SUPERVISOR_PORT}/"
    echo "VERO launch → ${url}"
    if command -v xdg-open >/dev/null 2>&1; then
      xdg-open "${url}" >/dev/null 2>&1 || true
    else
      python -c "import webbrowser; webbrowser.open('${url}')" || true
    fi
    ;;
  bootstrap)
    python bootstrap.py
    ;;
  preflight)
    python -m agents.control
    ;;
  smoke)
    python -c "
from bootstrap import bootstrap, autonomy_status
from engine.queue import QUEUE
st = autonomy_status()
if not st['ready']:
    bootstrap()
    st = autonomy_status()
if not st['ready']:
    raise SystemExit('bootstrap failed: ' + str(st.get('missing')))
print(QUEUE.start(universe='seed', timeframes=['1h'], strategies=['rsi_mean_reversion','ema_crossover'], candle_limit=200))
"
    ;;
  test)
    python -m pytest tests/ -q
    ;;
  signal|scan)
    python -c "
from engine.signal_scanner import run_scan
import json
print(json.dumps(run_scan(), indent=2, ensure_ascii=False))
"
    ;;
  telegram-discover)
    python -c "
from engine.telegram_notify import discover_chat_ids
import json
print(json.dumps(discover_chat_ids(), indent=2, ensure_ascii=False))
"
    ;;
  telegram-test)
    python -c "
from engine.telegram_test import test_strategies_telegram
import json
print(json.dumps(test_strategies_telegram(), indent=2, ensure_ascii=False))
"
    ;;
  *)
    echo "Usage: $0 [server|guard|open|bootstrap|preflight|smoke|test|signal|telegram-discover|telegram-test]"
    exit 1
    ;;
esac
