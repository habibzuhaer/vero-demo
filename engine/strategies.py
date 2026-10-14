"""Стратегии сигналов (логика как в TRADE)."""

from __future__ import annotations

from typing import Callable

import pandas as pd

from config import (
    BB_EXIT_TO_MID,
    BB_SQUEEZE_LOOKBACK,
    BB_SQUEEZE_PCT,
    CCI_OB,
    CCI_OS,
    RSI_OB,
    RSI_OS,
    STOCH_OB,
    STOCH_OS,
    STRATEGY_IDS,
    WILLR_OB,
    WILLR_OS,
)


def _pack(index, buy, sell) -> pd.Series:
    sig = pd.Series(0, index=index, dtype=int)
    sig = sig.mask(buy, 1).mask(sell, -1)
    return sig.fillna(0).astype(int)


def _rsi(df: pd.DataFrame) -> pd.Series:
    rsi = df["RSI"]
    prev = rsi.shift(1)
    buy = (prev < RSI_OS) & (rsi >= RSI_OS)
    sell = (prev > RSI_OB) & (rsi <= RSI_OB)
    return _pack(df.index, buy, sell)


def _macd(df: pd.DataFrame) -> pd.Series:
    macd = df["MACD"]
    signal = df["MACD_SIGNAL"]
    prev_m = macd.shift(1)
    prev_s = signal.shift(1)
    buy = (prev_m <= prev_s) & (macd > signal)
    sell = (prev_m >= prev_s) & (macd < signal)
    return _pack(df.index, buy, sell)


def _bb(df: pd.DataFrame) -> pd.Series:
    width = df["BB_WIDTH"]
    lookback = BB_SQUEEZE_LOOKBACK
    q = width.shift(1).rolling(lookback, min_periods=max(5, lookback // 2)).quantile(
        BB_SQUEEZE_PCT
    )
    in_squeeze = width.shift(1) <= q
    close = df["close"]
    prev_close = close.shift(1)
    upper = df["BB_UPPER"]
    mid = df["BB_MID"]
    buy = in_squeeze & (prev_close <= upper.shift(1)) & (close > upper)
    if BB_EXIT_TO_MID:
        sell = (prev_close >= mid.shift(1)) & (close < mid)
    else:
        sell = close < df["BB_LOWER"]
    return _pack(df.index, buy, sell)


def _stoch(df: pd.DataFrame) -> pd.Series:
    k = df["STOCH_K"]
    d = df["STOCH_D"]
    prev_k = k.shift(1)
    prev_d = d.shift(1)
    buy = (prev_k <= prev_d) & (k > d) & (k < STOCH_OS + 10)
    sell = (prev_k >= prev_d) & (k < d) & (k > STOCH_OB - 10)
    return _pack(df.index, buy, sell)


def _ema(df: pd.DataFrame) -> pd.Series:
    fast = df["EMA_FAST"]
    slow = df["EMA_SLOW"]
    prev_f = fast.shift(1)
    prev_s = slow.shift(1)
    buy = (prev_f <= prev_s) & (fast > slow)
    sell = (prev_f >= prev_s) & (fast < slow)
    return _pack(df.index, buy, sell)


def _cci(df: pd.DataFrame) -> pd.Series:
    cci = df["CCI"]
    prev = cci.shift(1)
    buy = (prev < CCI_OS) & (cci >= CCI_OS)
    sell = (prev > CCI_OB) & (cci <= CCI_OB)
    return _pack(df.index, buy, sell)


def _willr(df: pd.DataFrame) -> pd.Series:
    w = df["WILLR"]
    prev = w.shift(1)
    buy = (prev < WILLR_OS) & (w >= WILLR_OS)
    sell = (prev > WILLR_OB) & (w <= WILLR_OB)
    return _pack(df.index, buy, sell)


_MAP: dict[str, Callable[[pd.DataFrame], pd.Series]] = {
    "rsi_mean_reversion": _rsi,
    "macd_crossover": _macd,
    "bb_squeeze": _bb,
    "stoch_mean_reversion": _stoch,
    "ema_crossover": _ema,
    "cci_mean_reversion": _cci,
    "willr_mean_reversion": _willr,
}


def generate(df: pd.DataFrame, strategy_id: str) -> pd.DataFrame:
    if strategy_id not in _MAP:
        raise ValueError(f"Unknown strategy {strategy_id}. Allowed: {STRATEGY_IDS}")
    out = df.copy()
    out["signal"] = _MAP[strategy_id](out)
    out["strategy"] = strategy_id
    return out
