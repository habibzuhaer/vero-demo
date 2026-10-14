"""Индикаторы без pandas_ta — совместимость с любым Python."""

from __future__ import annotations

import pandas as pd

from config import (
    BB_LENGTH,
    BB_STD,
    CCI_LENGTH,
    EMA_FAST,
    EMA_SLOW,
    MACD_FAST,
    MACD_SIGNAL,
    MACD_SLOW,
    RSI_LENGTH,
    STOCH_D,
    STOCH_K,
    STOCH_SMOOTH,
    WILLR_LENGTH,
)


def _ema(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(span=length, adjust=False).mean()


def _rsi(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def enrich(ohlcv: pd.DataFrame) -> pd.DataFrame:
    df = ohlcv.copy()
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    df["RSI"] = _rsi(close, RSI_LENGTH)

    ema_fast = _ema(close, MACD_FAST)
    ema_slow = _ema(close, MACD_SLOW)
    macd = ema_fast - ema_slow
    signal = _ema(macd, MACD_SIGNAL)
    df["MACD"] = macd
    df["MACD_SIGNAL"] = signal
    df["MACD_HIST"] = macd - signal

    mid = close.rolling(BB_LENGTH).mean()
    std = close.rolling(BB_LENGTH).std()
    df["BB_MID"] = mid
    df["BB_UPPER"] = mid + BB_STD * std
    df["BB_LOWER"] = mid - BB_STD * std
    df["BB_WIDTH"] = (df["BB_UPPER"] - df["BB_LOWER"]) / mid.replace(0, pd.NA)

    lowest = low.rolling(STOCH_K).min()
    highest = high.rolling(STOCH_K).max()
    raw_k = 100 * (close - lowest) / (highest - lowest).replace(0, pd.NA)
    k = raw_k.rolling(STOCH_SMOOTH).mean()
    d = k.rolling(STOCH_D).mean()
    df["STOCH_K"] = k
    df["STOCH_D"] = d

    df["EMA_FAST"] = _ema(close, EMA_FAST)
    df["EMA_SLOW"] = _ema(close, EMA_SLOW)

    tp = (high + low + close) / 3.0
    sma = tp.rolling(CCI_LENGTH).mean()
    mad = (tp - sma).abs().rolling(CCI_LENGTH).mean()
    df["CCI"] = (tp - sma) / (0.015 * mad.replace(0, pd.NA))

    hh = high.rolling(WILLR_LENGTH).max()
    ll = low.rolling(WILLR_LENGTH).min()
    df["WILLR"] = -100 * (hh - close) / (hh - ll).replace(0, pd.NA)

    return df
