"""VERO — автономные пути (всё внутри проекта)."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def _load_dotenv() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
    except ImportError:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_dotenv()
CATALOG_DIR = DATA_DIR / "catalog"
CANDLES_DIR = DATA_DIR / "candles"
CACHE_DIR = DATA_DIR / "cache"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "vero.db"
WEB_DIR = ROOT / "web"

# Локальные данные (после bootstrap)
XTB_CACHE = CATALOG_DIR / "xtb_symbols_cache.json"
XTB_QUALIFIED = CATALOG_DIR / "xtb_qualified_latest.json"
XTB_TOP = CATALOG_DIR / "xtb_top_pairs.json"
CANDLES_DB = CANDLES_DIR / "vero_candles.db"
# Своя БД VERO: OHLCV + индикаторы (не MTB)
VERO_BARS_DB = CANDLES_DIR / "vero_bars.db"

# Опциональный источник для одноразового bootstrap (не runtime-зависимость)
BOOTSTRAP_MTB_ROOT = Path(
    os.environ.get(
        "VERO_BOOTSTRAP_FROM",
        os.environ.get(
            "MTB_ROOT",
            "./sample_data",
        ),
    )
)

HOST = os.environ.get("VERO_HOST", "127.0.0.1")
PORT = int(os.environ.get("VERO_PORT", "8900"))
# Отдельный guard-процесс (агенты в циклах + launch.html)
SUPERVISOR_HOST = os.environ.get("VERO_SUPERVISOR_HOST", HOST)
SUPERVISOR_PORT = int(os.environ.get("VERO_SUPERVISOR_PORT", "8901"))

ALLOWED_TIMEFRAMES: tuple[str, ...] = ("30m", "1h", "4h", "1d")
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("1h", "4h", "1d")
DEFAULT_CANDLE_LIMIT = 1600
CANDLE_LIMIT_UI_MIN = 800
CANDLE_LIMIT_UI_MAX = 8000
CANDLE_LIMIT_PRESETS: tuple[int, ...] = (800, 1000, 1600, 2000, 3000, 4000, 5000, 6000, 8000)
MIN_CANDLES = 80
PREFETCH_TARGET_BARS = int(os.environ.get("VERO_PREFETCH_BARS", "1600"))
PREFETCH_TIMEFRAMES: tuple[str, ...] = tuple(
    x.strip()
    for x in os.environ.get("VERO_PREFETCH_TFS", "1h,4h,1d").split(",")
    if x.strip()
) or ("1h", "4h", "1d")
_OHLCV_ORDER_RAW_DEFAULT = "vero,local,tv,yahoo,stooq"

STRATEGY_IDS: tuple[str, ...] = (
    "rsi_mean_reversion",
    "macd_crossover",
    "bb_squeeze",
    "stoch_mean_reversion",
    "ema_crossover",
    "cci_mean_reversion",
    "willr_mean_reversion",
)

DEFAULT_CASH = 10.0
DEFAULT_FEE_RATE = 0.001
DEFAULT_WF_TRAIN_RATIO = 0.70
DEFAULT_STOP_LOSS_PCT: float | None = None
STOP_LOSS_MIN = 0.5
STOP_LOSS_MAX = 3.0

RSI_LENGTH = 14
RSI_OS = 30.0
RSI_OB = 80.0
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BB_LENGTH = 20
BB_STD = 2.0
BB_SQUEEZE_LOOKBACK = 20
BB_SQUEEZE_PCT = 0.20
BB_EXIT_TO_MID = True
STOCH_K = 14
STOCH_D = 3
STOCH_SMOOTH = 3
STOCH_OS = 20.0
STOCH_OB = 80.0
EMA_FAST = 9
EMA_SLOW = 21
CCI_LENGTH = 20
CCI_OS = -100.0
CCI_OB = 100.0
WILLR_LENGTH = 14
WILLR_OS = -80.0
WILLR_OB = -20.0

DAILY_RUN_HOURS_MIN = float(os.environ.get("VERO_DAILY_HOURS_MIN", "4"))
DAILY_RUN_HOURS_MAX = float(os.environ.get("VERO_DAILY_HOURS_MAX", "8"))
if DAILY_RUN_HOURS_MAX < DAILY_RUN_HOURS_MIN:
    DAILY_RUN_HOURS_MAX = DAILY_RUN_HOURS_MIN
DAILY_RUN_HOURS = DAILY_RUN_HOURS_MIN

PAUSE_LOCAL_MIN = float(os.environ.get("VERO_PAUSE_LOCAL_MIN", "0.12"))
PAUSE_LOCAL_MAX = float(os.environ.get("VERO_PAUSE_LOCAL_MAX", "0.45"))
PAUSE_YAHOO_MIN = float(os.environ.get("VERO_PAUSE_YAHOO_MIN", "1.2"))
PAUSE_YAHOO_MAX = float(os.environ.get("VERO_PAUSE_YAHOO_MAX", "4.5"))
PAUSE_TV_MIN = float(os.environ.get("VERO_PAUSE_TV_MIN", "2.8"))
PAUSE_TV_MAX = float(os.environ.get("VERO_PAUSE_TV_MAX", "6.5"))
PAUSE_STOOQ_MIN = float(os.environ.get("VERO_PAUSE_STOOQ_MIN", "1.8"))
PAUSE_STOOQ_MAX = float(os.environ.get("VERO_PAUSE_STOOQ_MAX", "4.2"))

YAHOO_PAUSE_SEC = float(os.environ.get("VERO_YAHOO_PAUSE", "0.8"))
YAHOO_MIN_INTERVAL_MIN = float(os.environ.get("VERO_YAHOO_INTERVAL_MIN", "1.0"))
YAHOO_MIN_INTERVAL_MAX = float(os.environ.get("VERO_YAHOO_INTERVAL_MAX", "3.5"))
TV_MIN_INTERVAL_MIN = float(os.environ.get("VERO_TV_INTERVAL_MIN", "2.5"))
TV_MIN_INTERVAL_MAX = float(os.environ.get("VERO_TV_INTERVAL_MAX", "5.5"))
STOOQ_MIN_INTERVAL_MIN = float(os.environ.get("VERO_STOOQ_INTERVAL_MIN", "1.5"))
STOOQ_MIN_INTERVAL_MAX = float(os.environ.get("VERO_STOOQ_INTERVAL_MAX", "3.8"))
YAHOO_BACKOFF_BASE_SEC = float(os.environ.get("VERO_YAHOO_BACKOFF_BASE", "8"))
YAHOO_BACKOFF_MAX_SEC = float(os.environ.get("VERO_YAHOO_BACKOFF_MAX", "120"))
YAHOO_MAX_WORKERS = int(os.environ.get("VERO_YAHOO_WORKERS", "1"))

QUEUE_BATCH_PAUSE_SEC = PAUSE_LOCAL_MIN
QUEUE_YAHOO_PAUSE_SEC = PAUSE_YAHOO_MIN

YAHOO_FALLBACK = os.environ.get("VERO_YAHOO_FALLBACK", "false").strip().lower() in (
    "1", "true", "yes",
)
YAHOO_FIRST = os.environ.get("VERO_YAHOO_FIRST", "false").strip().lower() in (
    "1", "true", "yes",
)
REQUIRE_LOCAL_CANDLES = os.environ.get("VERO_REQUIRE_LOCAL", "true").strip().lower() in (
    "1", "true", "yes",
)
YAHOO_CACHE_MAX_AGE_SEC = int(os.environ.get("VERO_YAHOO_CACHE_MAX_AGE", "3600"))
TV_ENABLED = os.environ.get("VERO_TV", "true").strip().lower() in (
    "1", "true", "yes",
)
STOOQ_ENABLED = os.environ.get("VERO_STOOQ", "true").strip().lower() in (
    "1", "true", "yes",
)
_OHLCV_ORDER_RAW = os.environ.get("VERO_OHLCV_ORDER", _OHLCV_ORDER_RAW_DEFAULT)
OHLCV_SOURCE_ORDER: tuple[str, ...] = tuple(
    x.strip().lower()
    for x in _OHLCV_ORDER_RAW.split(",")
    if x.strip()
) or ("vero", "local", "tv", "yahoo", "stooq")
DEFAULT_SAVE_TRADES = os.environ.get("VERO_SAVE_TRADES", "false").strip().lower() in (
    "1", "true", "yes",
)
EXCLUDE_NON_STOCKS = True

TOP_MIN_TRADES = 4
TOP_MIN_OOS_TRADES = 2
LEARN_N_CLUSTERS = 8
LEARN_MIN_SAMPLES = 30

# Telegram signals + paper trading
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SIGNAL_SCAN_ENABLED = os.environ.get("VERO_SIGNAL_SCAN", "false").strip().lower() in (
    "1", "true", "yes",
)
SIGNAL_SCAN_INTERVAL_SEC = int(os.environ.get("VERO_SIGNAL_INTERVAL_SEC", "300"))
SIGNAL_CANDLE_LIMIT = int(os.environ.get("VERO_SIGNAL_CANDLES", "400"))
PAPER_INITIAL_CASH = float(os.environ.get("VERO_PAPER_CASH", "10000"))
PAPER_FEE_RATE = float(os.environ.get("VERO_PAPER_FEE", "0.001"))
SIGNAL_WATCH = os.environ.get("VERO_SIGNAL_WATCH", "both")  # user | auto | both
SIGNAL_MIN_OOS = float(os.environ.get("VERO_SIGNAL_MIN_OOS", "0"))
CHARTS_DIR = EXPORTS_DIR / "charts"

DEFAULT_RISK = {
    "max_risk_pct_per_trade": 1.0,
    "max_daily_loss_pct": 3.0,
    "max_positions": 3,
    "min_winrate_pct": 35.0,
    "min_profit_factor": 1.15,
    "max_dd_pct": 25.0,
    "require_stop_loss": True,
    "default_stop_loss_pct": 1.5,
}

SIGNAL_DEFAULT_STOP_LOSS_PCT = float(
    os.environ.get(
        "VERO_SIGNAL_STOP_LOSS_PCT",
        str(DEFAULT_RISK["default_stop_loss_pct"]),
    )
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, CATALOG_DIR, CANDLES_DIR, CACHE_DIR, EXPORTS_DIR, CHARTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
