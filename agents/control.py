"""Агенты контроля модулей VERO — preflight перед бэктестом."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass
class CheckResult:
    module: str
    ok: bool
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BaseControlAgent:
    name: str = "base"

    def check(self) -> CheckResult:
        raise NotImplementedError


class ConfigAgent(BaseControlAgent):
    name = "config"

    def check(self) -> CheckResult:
        from config import (
            ALLOWED_TIMEFRAMES,
            STRATEGY_IDS,
            DB_PATH,
            DATA_DIR,
            WEB_DIR,
        )

        missing = []
        if not ALLOWED_TIMEFRAMES:
            missing.append("ALLOWED_TIMEFRAMES")
        if len(STRATEGY_IDS) < 7:
            missing.append("STRATEGY_IDS")
        if not WEB_DIR.exists():
            missing.append("WEB_DIR")
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return CheckResult(
            self.name,
            ok=not missing,
            message="ok" if not missing else f"missing {missing}",
            details={
                "tfs": list(ALLOWED_TIMEFRAMES),
                "strategies": list(STRATEGY_IDS),
                "db": str(DB_PATH),
            },
        )


class DbAgent(BaseControlAgent):
    name = "db"

    def check(self) -> CheckResult:
        from db import init_db, db_session

        try:
            init_db()
            with db_session() as conn:
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
                ).fetchone()["n"]
                needed = {
                    "runs",
                    "run_jobs",
                    "results",
                    "trades",
                    "user_top",
                    "learn_samples",
                    "learn_clusters",
                    "risk_rules",
                }
                tables = {
                    r[0]
                    for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                miss = sorted(needed - tables)
            return CheckResult(
                self.name,
                ok=not miss,
                message="ok" if not miss else f"missing tables {miss}",
                details={"tables": n, "missing": miss},
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class CandlesAgent(BaseControlAgent):
    name = "candles"

    def check(self) -> CheckResult:
        from engine.local_candles import (
            fetch_local_ohlcv,
            local_candles_available,
            local_symbol_count,
        )

        if not local_candles_available():
            return CheckResult(self.name, False, "local candles db missing")
        try:
            n1h = local_symbol_count("1h")
            df, err = fetch_local_ohlcv("WULF.US", "1h", limit=120)
            ok = err is None and len(df) >= 80
            return CheckResult(
                self.name,
                ok=ok,
                message="ok" if ok else (err or "short history"),
                details={
                    "symbols_1h": n1h,
                    "sample_bars": len(df),
                    "cols": list(df.columns) if not df.empty else [],
                },
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class IndicatorsAgent(BaseControlAgent):
    name = "indicators"

    def check(self) -> CheckResult:
        import pandas as pd
        from engine.indicators import enrich

        try:
            n = 120
            close = pd.Series(range(n), dtype=float) + 100
            df = pd.DataFrame(
                {
                    "timestamp": list(range(n)),
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": [1.0] * n,
                }
            )
            out = enrich(df)
            need = ["RSI", "MACD", "BB_WIDTH", "STOCH_K", "EMA_FAST", "CCI", "WILLR"]
            miss = [c for c in need if c not in out.columns]
            return CheckResult(
                self.name,
                ok=not miss,
                message="ok" if not miss else f"missing {miss}",
                details={"rows": len(out)},
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class StrategiesAgent(BaseControlAgent):
    name = "strategies"

    def check(self) -> CheckResult:
        import pandas as pd
        import numpy as np
        from config import STRATEGY_IDS
        from engine.indicators import enrich
        from engine import strategies

        try:
            rng = np.random.default_rng(42)
            n = 200
            close = 100 + np.cumsum(rng.normal(0, 1, n))
            df = pd.DataFrame(
                {
                    "timestamp": list(range(n)),
                    "open": close,
                    "high": close + 1,
                    "low": close - 1,
                    "close": close,
                    "volume": rng.random(n) * 1000,
                }
            )
            enriched = enrich(df)
            bad = []
            for sid in STRATEGY_IDS:
                out = strategies.generate(enriched, sid)
                if "signal" not in out.columns:
                    bad.append(sid)
                    continue
                vals = set(int(x) for x in out["signal"].dropna().unique())
                if not vals.issubset({-1, 0, 1}):
                    bad.append(sid)
            return CheckResult(
                self.name,
                ok=not bad,
                message="ok" if not bad else f"bad {bad}",
                details={"strategies": len(STRATEGY_IDS)},
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class ExecutionAgentCtrl(BaseControlAgent):
    name = "execution"

    def check(self) -> CheckResult:
        import pandas as pd
        from engine.execution import run_backtest

        try:
            n = 50
            df = pd.DataFrame(
                {
                    "timestamp": list(range(n)),
                    "open": [10.0] * n,
                    "high": [10.5] * n,
                    "low": [9.5] * n,
                    "close": [10.0 + (0.1 if i % 10 == 0 else 0) for i in range(n)],
                    "volume": [1.0] * n,
                    "signal": [1 if i == 5 else (-1 if i == 20 else 0) for i in range(n)],
                }
            )
            res = run_backtest(df, cash=10.0, fee_rate=0.001, train_ratio=0.7)
            ok = "return_pct" in res.metrics_full and len(res.trades) >= 2
            return CheckResult(
                self.name,
                ok=ok,
                message="ok" if ok else "no trades/metrics",
                details={
                    "trades": len(res.trades),
                    "metrics": res.metrics_full,
                },
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class BacktestPipelineAgent(BaseControlAgent):
    name = "backtest_pipeline"

    def check(self) -> CheckResult:
        from engine.backtest import run_one

        try:
            out = run_one(
                "WULF.US",
                "1h",
                "ema_crossover",
                limit=150,
                cash=10.0,
                fee_rate=0.001,
                train_ratio=0.7,
                stop_loss_pct=None,
            )
            ok = bool(out.get("ok"))
            return CheckResult(
                self.name,
                ok=ok,
                message="ok" if ok else str(out.get("error")),
                details={
                    "source": out.get("source"),
                    "candles": out.get("candles"),
                    "return_pct": out.get("return_pct"),
                },
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class LearnAgent(BaseControlAgent):
    name = "learn"

    def check(self) -> CheckResult:
        from learn.patterns import FEATURE_KEYS, _label, _rule_from_stats

        try:
            lab = _label(
                {
                    "oos_return_pct": 5.0,
                    "oos_profit_factor": 1.5,
                    "oos_max_dd_pct": 10.0,
                    "oos_closed": 5,
                }
            )
            rule = _rule_from_stats(
                0,
                {
                    "good_pct": 50,
                    "avg_oos_return_pct": 3.0,
                    "avg_oos_pf": 1.4,
                    "avg_oos_dd": 12.0,
                },
            )
            ok = lab == "good" and rule.get("action") == "allow" and len(FEATURE_KEYS) > 5
            return CheckResult(
                self.name,
                ok=ok,
                message="ok" if ok else "label/rule mismatch",
                details={"label": lab, "action": rule.get("action")},
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class CatalogAgent(BaseControlAgent):
    name = "catalog"

    def check(self) -> CheckResult:
        from engine.yahoo import catalog_stats, universe_symbols, xtb_to_yahoo

        try:
            st = catalog_stats()
            seed = universe_symbols("seed")
            ok = st.get("cache_exists") and st.get("full", 0) > 100 and len(seed) > 0
            return CheckResult(
                self.name,
                ok=ok,
                message="ok" if ok else "catalog incomplete",
                details={**st, "seed_sample": seed[:5], "map_wulf": xtb_to_yahoo("WULF.US")},
            )
        except Exception as e:
            return CheckResult(self.name, False, str(e))


class WebAgent(BaseControlAgent):
    name = "web"

    def check(self) -> CheckResult:
        from config import WEB_DIR

        need = [
            WEB_DIR / "index.html",
            WEB_DIR / "static" / "app.js",
            WEB_DIR / "static" / "app.css",
        ]
        miss = [str(p) for p in need if not p.exists()]
        return CheckResult(
            self.name,
            ok=not miss,
            message="ok" if not miss else f"missing {miss}",
            details={"web_dir": str(WEB_DIR)},
        )


CONTROL_AGENTS: list[BaseControlAgent] = [
    ConfigAgent(),
    DbAgent(),
    CatalogAgent(),
    CandlesAgent(),
    IndicatorsAgent(),
    StrategiesAgent(),
    ExecutionAgentCtrl(),
    BacktestPipelineAgent(),
    LearnAgent(),
    WebAgent(),
]


def run_preflight(*, only: list[str] | None = None) -> dict[str, Any]:
    agents = CONTROL_AGENTS
    if only:
        want = set(only)
        agents = [a for a in agents if a.name in want]
    results = [a.check() for a in agents]
    ok = all(r.ok for r in results)
    return {
        "ok": ok,
        "passed": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "checks": [r.to_dict() for r in results],
    }


def main() -> int:
    import json
    import sys

    report = run_preflight()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
