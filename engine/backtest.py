"""Один прогон: symbol × TF × strategy."""

from __future__ import annotations

from typing import Any

from config import YAHOO_FALLBACK, YAHOO_FIRST
from engine import execution, indicators, strategies
from engine.ohlcv import load_ohlcv
from engine.yahoo import is_excluded_symbol


def run_one(
    symbol: str,
    timeframe: str,
    strategy: str,
    *,
    limit: int,
    cash: float,
    fee_rate: float,
    train_ratio: float,
    stop_loss_pct: float | None,
    use_cache: bool = True,
) -> dict[str, Any]:
    if is_excluded_symbol(symbol):
        return {
            "ok": False,
            "skipped": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "yahoo_symbol": None,
            "error": "excluded (forex/index/commodity)",
            "source": "skip",
        }

    # OHLCV: Yahoo-first или local → Yahoo fallback
    df, yahoo, source, err = load_ohlcv(symbol, timeframe, limit)
    if err or df.empty:
        return {
            "ok": False,
            "skipped": source == "skip",
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy": strategy,
            "yahoo_symbol": yahoo or None,
            "error": err or "empty ohlcv",
            "source": source,
        }

    enriched = indicators.enrich(df)
    signaled = strategies.generate(enriched, strategy)
    bt = execution.run_backtest(
        signaled,
        cash=cash,
        fee_rate=fee_rate,
        train_ratio=train_ratio,
        stop_loss_pct=stop_loss_pct,
    )
    full = bt.metrics_full
    oos = bt.metrics_oos or {}

    entry_feats = _entry_features(signaled, bt.trades)

    return {
        "ok": True,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy": strategy,
        "yahoo_symbol": yahoo,
        "source": source,
        "candles": len(df),
        "return_pct": full.get("return_pct"),
        "oos_return_pct": oos.get("return_pct"),
        "winrate_pct": full.get("winrate_pct"),
        "oos_winrate_pct": oos.get("winrate_pct"),
        "profit_factor": full.get("profit_factor"),
        "oos_profit_factor": oos.get("profit_factor"),
        "max_dd_pct": full.get("max_dd_pct"),
        "oos_max_dd_pct": oos.get("max_dd_pct"),
        "trades": full.get("trades"),
        "closed_rounds": full.get("closed_rounds"),
        "oos_trades": oos.get("trades"),
        "oos_closed": oos.get("closed_rounds"),
        "stop_exits": full.get("stop_exits"),
        "final_usd": full.get("final_usd"),
        "trade_rows": [
            {
                "bar": t.bar,
                "timestamp": t.timestamp,
                "side": t.side,
                "price": t.price,
                "qty": t.qty,
                "fee": t.fee,
                "reason": t.reason,
            }
            for t in bt.trades
        ],
        "features": entry_feats,
    }


def _entry_features(df, trades) -> dict[str, Any]:
    buys = [t for t in trades if t.side == "BUY"]
    if not buys:
        return {}
    keys = ["RSI", "MACD_HIST", "BB_WIDTH", "STOCH_K", "CCI", "WILLR", "close"]
    acc: dict[str, list[float]] = {k: [] for k in keys}
    rets: list[float] = []
    for b in buys:
        if b.bar < 0 or b.bar >= len(df):
            continue
        row = df.iloc[b.bar]
        for k in keys:
            if k in df.columns and pd_notna(row[k]):
                acc[k].append(float(row[k]))
        sell = next((t for t in trades if t.side == "SELL" and t.bar > b.bar), None)
        if sell:
            rets.append((sell.price / b.price - 1.0) * 100.0)
    out: dict[str, Any] = {}
    for k, vals in acc.items():
        if vals:
            out[f"avg_{k.lower()}"] = round(sum(vals) / len(vals), 4)
    if rets:
        out["avg_trade_ret_pct"] = round(sum(rets) / len(rets), 4)
        out["n_rounds"] = len(rets)
    if "close" in df.columns and len(df) > 20:
        rets_bar = df["close"].pct_change().dropna()
        out["vol_20"] = round(float(rets_bar.tail(20).std() * 100), 4)
    return out


def pd_notna(v) -> bool:
    try:
        import math
        return v is not None and not (isinstance(v, float) and math.isnan(v))
    except Exception:
        return False
