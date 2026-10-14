"""Unit tests — ядро VERO без сети."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    monkeypatch.setattr("config.DB_PATH", db)
    monkeypatch.setattr("db.DB_PATH", db)
    from db import init_db

    init_db(db)
    return db


def _ohlcv(n: int = 150, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 50 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(n)],
            "open": close,
            "high": close + abs(rng.normal(0, 0.3, n)),
            "low": close - abs(rng.normal(0, 0.3, n)),
            "close": close,
            "volume": rng.random(n) * 1000,
        }
    )


def test_config_timeframes_and_strategies():
    from config import ALLOWED_TIMEFRAMES, DEFAULT_TIMEFRAMES, STRATEGY_IDS

    assert ALLOWED_TIMEFRAMES == ("30m", "1h", "4h", "1d")
    assert DEFAULT_TIMEFRAMES == ("1h", "4h", "1d")
    assert len(STRATEGY_IDS) == 7


def test_vero_store_roundtrip(tmp_path, monkeypatch):
    import config
    from engine import indicators
    from engine import vero_store as vs

    db = tmp_path / "vero_bars.db"
    monkeypatch.setattr(config, "VERO_BARS_DB", db)
    monkeypatch.setattr(vs, "VERO_BARS_DB", db)
    vs.ensure_store()
    raw = _ohlcv(200)
    en = indicators.enrich(raw)
    n = vs.replace_bars("TEST.US", "1h", en, source="test", keep_limit=200)
    assert n == 200
    out, err = vs.fetch_vero_bars("TEST.US", "1h", 200)
    assert err is None
    assert len(out) == 200
    assert "RSI" in out.columns and "MACD" in out.columns


def test_db_schema(tmp_db):
    from db import db_session

    with db_session(tmp_db) as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for t in ("runs", "results", "learn_samples", "risk_rules"):
        assert t in tables


def test_indicators_columns():
    from engine.indicators import enrich

    out = enrich(_ohlcv())
    for col in ("RSI", "MACD", "MACD_SIGNAL", "BB_UPPER", "BB_WIDTH", "STOCH_K", "EMA_FAST", "CCI", "WILLR"):
        assert col in out.columns


def test_all_strategies_emit_signals():
    from config import STRATEGY_IDS
    from engine.indicators import enrich
    from engine import strategies

    base = enrich(_ohlcv(250))
    for sid in STRATEGY_IDS:
        out = strategies.generate(base, sid)
        assert set(out["signal"].unique()).issubset({-1, 0, 1})


def test_execution_roundtrip_and_sl():
    from engine.execution import run_backtest

    df = _ohlcv(80)
    df["signal"] = 0
    df.loc[10, "signal"] = 1
    df.loc[40, "signal"] = -1
    res = run_backtest(df, cash=10, fee_rate=0.001, stop_loss_pct=None)
    assert res.metrics_full["trades"] >= 2
    assert "profit_factor" in res.metrics_full

    df2 = df.copy()
    # force SL: buy then crash
    df2["signal"] = 0
    df2.loc[5, "signal"] = 1
    df2.loc[6:, "low"] = df2.loc[5, "close"] * 0.9
    res2 = run_backtest(df2, cash=10, fee_rate=0.001, stop_loss_pct=1.0)
    assert any(t.reason == "stop_loss" for t in res2.trades)


def test_xtb_yahoo_mapping():
    from engine.yahoo import xtb_to_yahoo, is_forex_symbol, is_excluded_symbol, universe_symbols

    assert xtb_to_yahoo("WULF.US") == "WULF"
    assert xtb_to_yahoo("DFTK.DE") == "DFTK.DE"
    assert xtb_to_yahoo("VANTI.FR") == "VANTI.PA"
    assert xtb_to_yahoo("EURUSD") == "EURUSD=X"
    assert is_forex_symbol("EURUSD")
    assert is_forex_symbol("GBPUSD", "forex")
    assert is_excluded_symbol("US500")
    assert is_excluded_symbol("GOLD")
    assert not is_excluded_symbol("WULF.US")
    for kind in ("seed", "qualified", "top"):
        syms = universe_symbols(kind)
        assert not any(is_excluded_symbol(s) for s in syms)


def test_scheduler_budget():
    from engine.scheduler import add_active_seconds, set_session_budget, snapshot, _roll_new_day

    _roll_new_day()
    st = snapshot()
    assert st["budget_hours_min"] == 4.0
    assert st["budget_hours_max"] == 8.0
    assert 4.0 <= st["budget_hours"] <= 8.0
    assert st["remaining_sec"] > 0
    add_active_seconds(10)
    st2 = snapshot()
    assert st2["used_sec"] >= 10

    st4 = set_session_budget(4, label="4h")
    assert st4["session_mode"] == "fixed"
    assert st4["budget_hours"] == 4.0
    assert st4["remaining_hours"] == 4.0

    st_cycle = set_session_budget(None, label="cycle")
    assert st_cycle["session_mode"] == "unlimited"
    assert st_cycle["unlimited"] is True


def test_anti_ban_random_pause():
    from engine.anti_ban import job_pause, note_error, is_rate_error
    from config import PAUSE_LOCAL_MIN, PAUSE_LOCAL_MAX

    p = job_pause(source="local")
    assert PAUSE_LOCAL_MIN <= p <= PAUSE_LOCAL_MAX
    p_tv = job_pause(source="tv")
    from config import PAUSE_TV_MIN, PAUSE_TV_MAX

    assert PAUSE_TV_MIN <= p_tv <= PAUSE_TV_MAX
    assert is_rate_error("429 Too Many Requests")
    info = note_error("429 Too Many Requests")
    assert info["backoff_sec"] > 0


def test_learn_label_and_rules():
    from learn.patterns import _label, _rule_from_stats

    assert (
        _label(
            {
                "oos_return_pct": 4,
                "oos_profit_factor": 1.3,
                "oos_max_dd_pct": 10,
                "oos_closed": 4,
            }
        )
        == "good"
    )
    assert (
        _label(
            {
                "oos_return_pct": -10,
                "oos_profit_factor": 0.5,
                "oos_max_dd_pct": 50,
                "oos_closed": 4,
            }
        )
        == "bad"
    )
    rule = _rule_from_stats(
        1,
        {
            "good_pct": 10,
            "avg_oos_return_pct": -5,
            "avg_oos_pf": 0.7,
            "avg_oos_dd": 40,
        },
    )
    assert rule["action"] == "deny"


def test_control_preflight_core_agents():
    """Быстрые агенты без тяжёлого local DB sample — всегда должны пройти."""
    from agents.control import (
        ConfigAgent,
        IndicatorsAgent,
        StrategiesAgent,
        ExecutionAgentCtrl,
        LearnAgent,
        WebAgent,
        DbAgent,
    )

    for agent in (
        ConfigAgent(),
        DbAgent(),
        IndicatorsAgent(),
        StrategiesAgent(),
        ExecutionAgentCtrl(),
        LearnAgent(),
        WebAgent(),
    ):
        r = agent.check()
        assert r.ok, f"{agent.name}: {r.message}"


def test_local_candles_if_present():
    from engine.local_candles import local_candles_available, fetch_local_ohlcv

    if not local_candles_available():
        pytest.skip("no local candles db (run bootstrap)")
    df, err = fetch_local_ohlcv("WULF.US", "1h", 100)
    assert err is None
    assert len(df) >= 80
    assert {"open", "high", "low", "close", "volume"} <= set(df.columns)


def test_pipeline_smoke_if_candles():
    from engine.local_candles import local_candles_available
    from engine.backtest import run_one

    if not local_candles_available():
        pytest.skip("no local candles db (run bootstrap)")
    out = run_one(
        "WULF.US",
        "1h",
        "rsi_mean_reversion",
        limit=120,
        cash=10,
        fee_rate=0.001,
        train_ratio=0.7,
        stop_loss_pct=None,
    )
    assert out["ok"] is True
    assert out["source"] in ("vero", "local")
    assert out["candles"] >= 80


def test_tv_and_stooq_mapping():
    from engine.tv import xtb_to_tv_candidates
    from engine.stooq import xtb_to_stooq

    c = xtb_to_tv_candidates("AAPL.US")
    assert "NASDAQ:AAPL" in c
    assert "GPW:PKN" in xtb_to_tv_candidates("PKN.PL")
    assert any(x.startswith("XETR:") for x in xtb_to_tv_candidates("SAP.DE"))
    assert xtb_to_stooq("AAPL.US") == "aapl.us"
    assert xtb_to_stooq("AIR.FR") == "air.fr"
    assert xtb_to_stooq("EURUSD") is None


def test_ohlcv_cascade_prefers_local(monkeypatch):
    import pandas as pd
    from engine import ohlcv as mod

    df = pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(100)],
            "open": [1.0] * 100,
            "high": [1.1] * 100,
            "low": [0.9] * 100,
            "close": [1.0] * 100,
            "volume": [1.0] * 100,
        }
    )
    called = {"tv": 0, "yahoo": 0}

    monkeypatch.setattr(mod, "_try_local", lambda *a, **k: (df.copy(), "AAPL", None))

    def boom_tv(*a, **k):
        called["tv"] += 1
        return pd.DataFrame(), "", "no"

    def boom_yahoo(*a, **k):
        called["yahoo"] += 1
        return pd.DataFrame(), "", "no"

    monkeypatch.setattr(mod, "_try_tv", boom_tv)
    monkeypatch.setattr(mod, "_try_yahoo", boom_yahoo)
    monkeypatch.setattr(mod, "_FETCHERS", {
        "local": lambda *a, **k: (df.copy(), "AAPL", None),
        "tv": boom_tv,
        "yahoo": boom_yahoo,
        "stooq": boom_yahoo,
    })
    mod._mem.clear()
    out_df, mapped, source, err = mod.load_ohlcv("AAPL.US", "1h", 100, use_cache=True)
    assert err is None
    assert source == "local"
    assert len(out_df) >= 80
    assert called["tv"] == 0
    assert called["yahoo"] == 0
