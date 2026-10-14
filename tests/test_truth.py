"""Честный SL 1.5%: pairing сделок и сверка стоп/тейк по OHLC."""

from __future__ import annotations

import pandas as pd

from engine.truth import (
    TRUTH_SL_PCT,
    audit_round,
    first_stop_hit,
    pair_rounds,
    prefetch_ready,
    sl_price,
    summarize_rounds,
)


def _df(n: int = 20, entry: float = 100.0) -> pd.DataFrame:
    close = [entry] * n
    return pd.DataFrame(
        {
            "timestamp": [1_700_000_000_000 + i * 3_600_000 for i in range(n)],
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": close,
            "close": close,
            "volume": [1.0] * n,
        }
    )


def test_sl_price_is_1_5_pct():
    assert abs(sl_price(100.0) - 98.5) < 1e-9
    assert TRUTH_SL_PCT == 1.5


def test_pair_rounds_stop_and_take():
    trades = [
        {"side": "BUY", "price": 100, "bar": 1, "timestamp": 10, "reason": "signal"},
        {"side": "SELL", "price": 98.5, "bar": 3, "timestamp": 30, "reason": "stop_loss"},
        {"side": "BUY", "price": 100, "bar": 5, "timestamp": 50, "reason": "signal"},
        {"side": "SELL", "price": 104, "bar": 8, "timestamp": 80, "reason": "signal"},
    ]
    rounds = pair_rounds(trades)
    assert len(rounds) == 2
    assert rounds[0]["kind"] == "stop" and abs(rounds[0]["pnl_pct"] + 1.5) < 1e-6
    assert rounds[1]["kind"] == "take" and abs(rounds[1]["pnl_pct"] - 4.0) < 1e-6
    sm = summarize_rounds(rounds)
    assert sm["stops"] == 1 and sm["takes"] == 1
    assert sm["avg_take_pct"] == 4.0


def test_first_stop_hit_skips_entry_bar():
    df = _df(10, 100.0)
    df.loc[2, "low"] = 90.0  # entry bar if entry_i=2 — must ignore
    df.loc[4, "low"] = 98.0
    hit = first_stop_hit(df, entry_i=2, exit_i=6, stop_px=98.5)
    assert hit is not None
    assert hit["bar"] == 4
    assert hit["gap"] is False


def test_audit_stop_ok_and_take_ok():
    df = _df(12, 100.0)
    ts = list(df["timestamp"])
    df.loc[4, "low"] = 98.4
    stop_rnd = {
        "kind": "stop",
        "entry": 100.0,
        "exit": 98.5,
        "pnl_pct": -1.5,
        "stop_px": 98.5,
        "entry_ts": int(ts[2]),
        "exit_ts": int(ts[4]),
        "reason": "stop_loss",
    }
    take_df = _df(12, 100.0)
    take_rnd = {
        "kind": "take",
        "entry": 100.0,
        "exit": 103.0,
        "pnl_pct": 3.0,
        "stop_px": 98.5,
        "entry_ts": int(list(take_df["timestamp"])[2]),
        "exit_ts": int(list(take_df["timestamp"])[7]),
        "reason": "signal",
    }
    a1 = audit_round(stop_rnd, df)
    a2 = audit_round(take_rnd, take_df)
    assert a1["ok"] and a1["check"] in ("stop_ok", "gap")
    assert a2["ok"] and a2["check"] == "take_ok"


def test_audit_take_fails_if_path_hit_sl():
    df = _df(12, 100.0)
    ts = list(df["timestamp"])
    df.loc[5, "low"] = 98.0
    rnd = {
        "kind": "take",
        "entry": 100.0,
        "exit": 110.0,
        "pnl_pct": 10.0,
        "stop_px": 98.5,
        "entry_ts": int(ts[2]),
        "exit_ts": int(ts[8]),
        "reason": "signal",
    }
    out = audit_round(rnd, df)
    assert not out["ok"]
    assert out["check"] == "take_after_sl_path"


def test_prefetch_ready_gates():
    ok, _ = prefetch_ready({"status": "done", "total": 10, "done": 10})
    assert ok
    no, reason = prefetch_ready({"status": "running", "total": 10, "done": 3})
    assert not no
    assert "качает" in reason
    no2, _ = prefetch_ready(
        {"status": "stopped", "total": 100, "done": 40}, store_symbols=0
    )
    assert not no2
    ok2, reason2 = prefetch_ready(
        {"status": "stopped", "total": 100, "done": 40}, store_symbols=8495
    )
    assert ok2
    assert "8495" in reason2
