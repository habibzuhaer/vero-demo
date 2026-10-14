"""SQLite: runs, results, trades, user_top, learn features/clusters/rules."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import DB_PATH, DEFAULT_RISK

def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db_session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  tag TEXT UNIQUE NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  universe TEXT NOT NULL,
  timeframes TEXT NOT NULL,
  strategies TEXT NOT NULL,
  candle_limit INTEGER NOT NULL,
  cash REAL NOT NULL,
  fee_rate REAL NOT NULL,
  stop_loss_pct REAL,
  total_jobs INTEGER NOT NULL DEFAULT 0,
  done_jobs INTEGER NOT NULL DEFAULT 0,
  ok_jobs INTEGER NOT NULL DEFAULT 0,
  err_jobs INTEGER NOT NULL DEFAULT 0,
  cursor_idx INTEGER NOT NULL DEFAULT 0,
  message TEXT DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS run_jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  error TEXT,
  UNIQUE(run_id, idx),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id INTEGER NOT NULL,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  return_pct REAL,
  oos_return_pct REAL,
  winrate_pct REAL,
  oos_winrate_pct REAL,
  profit_factor REAL,
  oos_profit_factor REAL,
  max_dd_pct REAL,
  oos_max_dd_pct REAL,
  trades INTEGER,
  closed_rounds INTEGER,
  oos_trades INTEGER,
  oos_closed INTEGER,
  stop_exits INTEGER,
  final_usd REAL,
  candles INTEGER,
  yahoo_symbol TEXT,
  extras TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(run_id, symbol, timeframe, strategy),
  FOREIGN KEY(run_id) REFERENCES runs(id)
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  result_id INTEGER NOT NULL,
  bar INTEGER,
  timestamp INTEGER,
  side TEXT,
  price REAL,
  qty REAL,
  fee REAL,
  reason TEXT,
  FOREIGN KEY(result_id) REFERENCES results(id)
);

CREATE TABLE IF NOT EXISTS user_top (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  timeframe TEXT,
  strategy TEXT,
  note TEXT DEFAULT '',
  score REAL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(symbol, timeframe, strategy)
);

CREATE TABLE IF NOT EXISTS learn_samples (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  result_id INTEGER,
  run_id INTEGER,
  symbol TEXT,
  timeframe TEXT,
  strategy TEXT,
  features TEXT NOT NULL,
  label TEXT,
  cluster_id INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS learn_clusters (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version TEXT NOT NULL,
  cluster_id INTEGER NOT NULL,
  size INTEGER NOT NULL,
  centroid TEXT,
  stats TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(version, cluster_id)
);

CREATE TABLE IF NOT EXISTS risk_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  scope TEXT NOT NULL,           -- global | cluster | pattern
  scope_key TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(scope, scope_key, name)
);

CREATE TABLE IF NOT EXISTS scheduler_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_results_run ON results(run_id);
CREATE INDEX IF NOT EXISTS idx_results_sym ON results(symbol);
CREATE INDEX IF NOT EXISTS idx_results_score ON results(oos_return_pct);
CREATE INDEX IF NOT EXISTS idx_jobs_run ON run_jobs(run_id, status);
CREATE INDEX IF NOT EXISTS idx_learn_cluster ON learn_samples(cluster_id);

CREATE TABLE IF NOT EXISTS paper_account (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  cash REAL NOT NULL,
  initial_cash REAL NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  qty REAL NOT NULL,
  entry_price REAL NOT NULL,
  entry_ts INTEGER NOT NULL,
  stop_loss_pct REAL,
  stop_price REAL,
  take_profit_note TEXT DEFAULT '',
  signal_id INTEGER,
  features TEXT,
  opened_at TEXT NOT NULL,
  UNIQUE(symbol, timeframe, strategy)
);

CREATE TABLE IF NOT EXISTS paper_trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  side TEXT NOT NULL,
  price REAL NOT NULL,
  qty REAL NOT NULL,
  fee REAL DEFAULT 0,
  reason TEXT,
  pnl_pct REAL,
  pnl_usd REAL,
  stop_loss_pct REAL,
  stop_price REAL,
  take_profit_note TEXT,
  signal_id INTEGER,
  features TEXT,
  bar_ts INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  event_type TEXT NOT NULL,
  signal_from INTEGER,
  signal_to INTEGER,
  price REAL NOT NULL,
  stop_loss_pct REAL,
  stop_price REAL,
  take_profit_note TEXT,
  bar_ts INTEGER NOT NULL,
  indicators TEXT,
  chart_path TEXT,
  oos_return_pct REAL,
  telegram_sent INTEGER DEFAULT 0,
  telegram_error TEXT,
  paper_trade_id INTEGER,
  dedupe_key TEXT UNIQUE,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signal_bar_state (
  symbol TEXT NOT NULL,
  timeframe TEXT NOT NULL,
  strategy TEXT NOT NULL,
  last_bar_ts INTEGER NOT NULL,
  last_signal INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (symbol, timeframe, strategy)
);

CREATE INDEX IF NOT EXISTS idx_signal_events_ts ON signal_events(created_at);
CREATE INDEX IF NOT EXISTS idx_paper_trades_sym ON paper_trades(symbol);
"""


def init_db(path: Path | None = None) -> None:
    with db_session(path) as conn:
        conn.executescript(SCHEMA)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM risk_rules WHERE scope='global'"
        ).fetchone()
        if row and row["n"] == 0:
            now = _utc()
            conn.execute(
                """
                INSERT INTO risk_rules(scope, scope_key, name, enabled, payload, created_at, updated_at)
                VALUES('global', '', 'defaults', 1, ?, ?, ?)
                """,
                (json.dumps(DEFAULT_RISK), now, now),
            )
        prow = conn.execute("SELECT id FROM paper_account WHERE id=1").fetchone()
        if not prow:
            from config import PAPER_INITIAL_CASH

            now = _utc()
            conn.execute(
                """
                INSERT INTO paper_account(id, cash, initial_cash, updated_at)
                VALUES(1, ?, ?, ?)
                """,
                (PAPER_INITIAL_CASH, PAPER_INITIAL_CASH, now),
            )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {k: row[k] for k in row.keys()}


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [{k: r[k] for k in r.keys()} for r in rows]
