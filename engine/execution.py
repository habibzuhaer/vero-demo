"""Paper long-only execution + metrics (как TRADE + profit factor)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from config import (
    DEFAULT_CASH,
    DEFAULT_FEE_RATE,
    DEFAULT_STOP_LOSS_PCT,
    DEFAULT_WF_TRAIN_RATIO,
    STOP_LOSS_MAX,
    STOP_LOSS_MIN,
)


@dataclass
class Trade:
    bar: int
    timestamp: int
    side: str
    price: float
    qty: float
    fee: float
    cash_after: float
    reason: str


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    final_equity: float = 0.0
    metrics_full: dict[str, Any] = field(default_factory=dict)
    metrics_oos: dict[str, Any] = field(default_factory=dict)


def _norm_sl(value: float | None) -> float | None:
    if value is None or value <= 0:
        return None
    if value < STOP_LOSS_MIN or value > STOP_LOSS_MAX:
        raise ValueError(f"stop_loss_pct out of range: {value}")
    return float(value)


def _simulate(
    df: pd.DataFrame,
    *,
    cash0: float,
    fee_rate: float,
    stop_loss_pct: float | None,
) -> dict[str, Any]:
    cash = float(cash0)
    qty = 0.0
    entry = 0.0
    trades: list[Trade] = []
    equity: list[float] = []
    has_hl = "low" in df.columns and "high" in df.columns
    sl = stop_loss_pct

    for pos, row in enumerate(df.itertuples(index=False)):
        close = float(row.close)
        low = float(row.low) if has_hl else close
        signal = int(row.signal)
        ts = int(row.timestamp)
        mark = close

        if qty > 0 and sl is not None and entry > 0:
            stop_px = entry * (1.0 - sl / 100.0)
            just_bought = trades and trades[-1].side == "BUY" and trades[-1].bar == pos
            if not just_bought and low <= stop_px:
                fill = stop_px
                gross = qty * fill
                fee = gross * fee_rate
                cash = gross - fee
                trades.append(Trade(pos, ts, "SELL", fill, qty, fee, cash, "stop_loss"))
                qty = 0.0
                entry = 0.0
                mark = fill

        if signal == 1 and cash > 0 and qty == 0 and close > 0:
            fee = cash * fee_rate
            spend = cash - fee
            qty = spend / close
            entry = close
            trades.append(Trade(pos, ts, "BUY", close, qty, fee, 0.0, "signal"))
            cash = 0.0
        elif signal == -1 and qty > 0 and close > 0:
            gross = qty * close
            fee = gross * fee_rate
            cash = gross - fee
            trades.append(Trade(pos, ts, "SELL", close, qty, fee, cash, "signal"))
            qty = 0.0
            entry = 0.0

        equity.append(cash + qty * mark)

    return {"trades": trades, "equity": equity}


def _metrics(
    equity: list[float],
    trades: list[Trade],
    *,
    start_cash: float,
    start_bar: int,
) -> dict[str, Any]:
    empty = {
        "return_pct": 0.0,
        "final_usd": start_cash,
        "trades": 0,
        "closed_rounds": 0,
        "winrate_pct": 0.0,
        "max_dd_pct": 0.0,
        "profit_factor": 0.0,
        "stop_exits": 0,
        "start_bar": start_bar,
    }
    if not equity:
        return empty

    final = equity[-1]
    ret = (final / start_cash - 1.0) * 100.0
    peak = equity[0]
    max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            max_dd = max(max_dd, (peak - e) / peak * 100.0)

    wins = 0
    closed = 0
    stop_exits = 0
    gross_win = 0.0
    gross_loss = 0.0
    last_buy_price = None
    last_buy_fee = 0.0
    for t in trades:
        if t.side == "BUY":
            last_buy_price = t.price
            last_buy_fee = t.fee
        elif t.side == "SELL" and last_buy_price is not None:
            closed += 1
            if t.reason == "stop_loss":
                stop_exits += 1
            # approx pnl per unit ignoring qty scale via cash path
            pnl = (t.price - last_buy_price) / last_buy_price * 100.0
            # fee drag rough
            fee_drag = (last_buy_fee + t.fee)
            if t.price > last_buy_price:
                wins += 1
                gross_win += abs(pnl)
            else:
                gross_loss += abs(pnl)
            last_buy_price = None
            _ = fee_drag
    winrate = (wins / closed * 100.0) if closed else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    return {
        "return_pct": round(ret, 4),
        "final_usd": round(final, 6),
        "trades": len(trades),
        "closed_rounds": closed,
        "winrate_pct": round(winrate, 2),
        "max_dd_pct": round(max_dd, 4),
        "profit_factor": round(min(pf, 999.0), 4),
        "stop_exits": stop_exits,
        "start_bar": start_bar,
    }


def run_backtest(
    df: pd.DataFrame,
    *,
    cash: float = DEFAULT_CASH,
    fee_rate: float = DEFAULT_FEE_RATE,
    train_ratio: float = DEFAULT_WF_TRAIN_RATIO,
    stop_loss_pct: float | None = DEFAULT_STOP_LOSS_PCT,
) -> BacktestResult:
    if "signal" not in df.columns:
        raise ValueError("missing signal column")
    sl = _norm_sl(stop_loss_pct)
    sim = _simulate(df, cash0=cash, fee_rate=fee_rate, stop_loss_pct=sl)
    result = BacktestResult(
        trades=sim["trades"],
        equity_curve=sim["equity"],
        final_equity=sim["equity"][-1] if sim["equity"] else cash,
    )
    result.metrics_full = _metrics(
        sim["equity"], sim["trades"], start_cash=cash, start_bar=0
    )
    split = max(1, int(len(df) * train_ratio))
    oos_df = df.iloc[split:].reset_index(drop=True)
    if len(oos_df) > 0:
        oos = _simulate(oos_df, cash0=cash, fee_rate=fee_rate, stop_loss_pct=sl)
        result.metrics_oos = _metrics(
            oos["equity"], oos["trades"], start_cash=cash, start_bar=split
        )
    else:
        result.metrics_oos = {}
    return result
