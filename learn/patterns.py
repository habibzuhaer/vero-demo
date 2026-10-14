"""Сбор фич, кластеризация, правила риска для паттернов."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import numpy as np

from config import DEFAULT_RISK, LEARN_MIN_SAMPLES, LEARN_N_CLUSTERS
from db import db_session, rows_to_dicts

FEATURE_KEYS = [
    "return_pct",
    "oos_return_pct",
    "winrate_pct",
    "oos_winrate_pct",
    "profit_factor",
    "oos_profit_factor",
    "max_dd_pct",
    "oos_max_dd_pct",
    "closed_rounds",
    "oos_closed",
    "avg_rsi",
    "avg_macd_hist",
    "avg_bb_width",
    "avg_stoch_k",
    "avg_cci",
    "avg_willr",
    "vol_20",
    "avg_trade_ret_pct",
]


def label_sample(feats: dict[str, Any]) -> str:
    """Метка сетапа для обучения: good | neutral | bad | thin."""
    oos = float(feats.get("oos_return_pct") or 0)
    pf = float(feats.get("oos_profit_factor") or feats.get("profit_factor") or 0)
    dd = float(feats.get("oos_max_dd_pct") or feats.get("max_dd_pct") or 0)
    closed = int(feats.get("oos_closed") or feats.get("closed_rounds") or 0)
    if closed < 2:
        return "thin"
    if oos > 0 and pf >= 1.2 and dd < 25:
        return "good"
    if oos < -5 or pf < 0.8 or dd > 40:
        return "bad"
    return "neutral"


# backward-compatible alias
_label = label_sample


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _vec(feats: dict[str, Any]) -> list[float]:
    out: list[float] = []
    for k in FEATURE_KEYS:
        v = feats.get(k)
        try:
            out.append(float(v) if v is not None else 0.0)
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def build_clusters(
    *,
    n_clusters: int | None = None,
    run_id: int | None = None,
) -> dict[str, Any]:
    n_clusters = n_clusters or LEARN_N_CLUSTERS
    with db_session() as conn:
        if run_id:
            rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM learn_samples WHERE run_id=?", (run_id,)
                ).fetchall()
            )
        else:
            rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM learn_samples ORDER BY id DESC LIMIT 50000"
                ).fetchall()
            )

    if len(rows) < LEARN_MIN_SAMPLES:
        return {
            "ok": False,
            "error": f"need >= {LEARN_MIN_SAMPLES} samples, have {len(rows)}",
            "n": len(rows),
        }

    X = np.array([_vec(json.loads(r["features"])) for r in rows], dtype=float)
    # sanitize
    X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)
    # standardize
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std < 1e-9] = 1.0
    Xs = (X - mean) / std

    try:
        from sklearn.cluster import KMeans
    except ImportError as e:
        return {"ok": False, "error": f"sklearn missing: {e}"}

    k = min(n_clusters, len(rows))
    model = KMeans(n_clusters=k, n_init=10, random_state=42)
    labels = model.fit_predict(Xs)

    version = datetime.now(timezone.utc).strftime("v%Y%m%d_%H%M%S")
    now = _utc()
    clusters_out: list[dict[str, Any]] = []

    with db_session() as conn:
        for cid in range(k):
            mask = labels == cid
            idxs = np.where(mask)[0]
            subset = [rows[i] for i in idxs]
            stats = _cluster_stats(subset)
            centroid = {
                FEATURE_KEYS[i]: round(float(model.cluster_centers_[cid][i]), 4)
                for i in range(len(FEATURE_KEYS))
            }
            conn.execute(
                """
                INSERT INTO learn_clusters(version, cluster_id, size, centroid, stats, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(version, cluster_id) DO UPDATE SET
                  size=excluded.size, centroid=excluded.centroid,
                  stats=excluded.stats, created_at=excluded.created_at
                """,
                (
                    version,
                    cid,
                    int(mask.sum()),
                    json.dumps(centroid),
                    json.dumps(stats),
                    now,
                ),
            )
            # update samples
            for i in idxs:
                conn.execute(
                    "UPDATE learn_samples SET cluster_id=? WHERE id=?",
                    (int(cid), rows[i]["id"]),
                )
            # auto risk rule per cluster
            rule = _rule_from_stats(cid, stats)
            conn.execute(
                """
                INSERT INTO risk_rules(scope, scope_key, name, enabled, payload, created_at, updated_at)
                VALUES ('cluster', ?, ?, 1, ?, ?, ?)
                ON CONFLICT(scope, scope_key, name) DO UPDATE SET
                  payload=excluded.payload, updated_at=excluded.updated_at, enabled=1
                """,
                (
                    str(cid),
                    f"cluster_{cid}_auto",
                    json.dumps(rule),
                    now,
                    now,
                ),
            )
            clusters_out.append(
                {
                    "cluster_id": cid,
                    "size": int(mask.sum()),
                    "stats": stats,
                    "rule": rule,
                }
            )

    return {
        "ok": True,
        "version": version,
        "n_samples": len(rows),
        "n_clusters": k,
        "clusters": clusters_out,
    }


def _cluster_stats(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        return {}
    feats = [json.loads(s["features"]) for s in samples]
    labels = [s.get("label") or "neutral" for s in samples]

    def avg(key: str) -> float:
        vals = []
        for f in feats:
            try:
                vals.append(float(f.get(key) or 0))
            except (TypeError, ValueError):
                pass
        return round(sum(vals) / len(vals), 4) if vals else 0.0

    good = sum(1 for l in labels if l == "good")
    bad = sum(1 for l in labels if l == "bad")
    return {
        "n": len(samples),
        "good_pct": round(good / len(samples) * 100, 1),
        "bad_pct": round(bad / len(samples) * 100, 1),
        "avg_oos_return_pct": avg("oos_return_pct"),
        "avg_oos_pf": avg("oos_profit_factor"),
        "avg_oos_dd": avg("oos_max_dd_pct"),
        "avg_winrate": avg("oos_winrate_pct"),
        "strategies": _top_counts([s.get("strategy") for s in samples]),
        "timeframes": _top_counts([s.get("timeframe") for s in samples]),
    }


def _top_counts(items: list[Any], n: int = 5) -> dict[str, int]:
    c: dict[str, int] = {}
    for x in items:
        if not x:
            continue
        c[str(x)] = c.get(str(x), 0) + 1
    return dict(sorted(c.items(), key=lambda kv: -kv[1])[:n])


def _rule_from_stats(cid: int, stats: dict[str, Any]) -> dict[str, Any]:
    """Правило риска под кластер: allow/deny + лимиты."""
    good = float(stats.get("good_pct") or 0)
    oos = float(stats.get("avg_oos_return_pct") or 0)
    pf = float(stats.get("avg_oos_pf") or 0)
    dd = float(stats.get("avg_oos_dd") or 0)

    if good >= 45 and oos > 0 and pf >= 1.2 and dd < 30:
        action = "allow"
        size_mult = 1.0
    elif good >= 30 and oos >= -2 and pf >= 1.0:
        action = "allow_reduced"
        size_mult = 0.5
    else:
        action = "deny"
        size_mult = 0.0

    return {
        "cluster_id": cid,
        "action": action,
        "size_mult": size_mult,
        "max_risk_pct_per_trade": DEFAULT_RISK["max_risk_pct_per_trade"] * max(size_mult, 0.25)
        if action != "deny"
        else 0.0,
        "require_stop_loss": True,
        "stop_loss_pct": DEFAULT_RISK["default_stop_loss_pct"],
        "min_oos_pf": 1.15 if action == "allow" else 1.0,
        "max_dd_pct": 25.0,
        "rationale": (
            f"good={good}%, avg_oos={oos}%, pf={pf}, dd={dd}% → {action}"
        ),
    }


def list_clusters(version: str | None = None) -> dict[str, Any]:
    with db_session() as conn:
        if version:
            rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM learn_clusters WHERE version=? ORDER BY cluster_id",
                    (version,),
                ).fetchall()
            )
        else:
            latest = conn.execute(
                "SELECT version FROM learn_clusters ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not latest:
                return {"version": None, "clusters": []}
            version = latest["version"]
            rows = rows_to_dicts(
                conn.execute(
                    "SELECT * FROM learn_clusters WHERE version=? ORDER BY cluster_id",
                    (version,),
                ).fetchall()
            )
    clusters = []
    for r in rows:
        clusters.append(
            {
                **r,
                "centroid": json.loads(r["centroid"] or "{}"),
                "stats": json.loads(r["stats"] or "{}"),
            }
        )
    return {"version": version, "clusters": clusters}


def list_rules() -> list[dict[str, Any]]:
    with db_session() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM risk_rules ORDER BY scope, scope_key, name"
            ).fetchall()
        )
    out = []
    for r in rows:
        out.append({**r, "payload": json.loads(r["payload"] or "{}")})
    return out


def upsert_rule(
    *,
    scope: str,
    scope_key: str,
    name: str,
    payload: dict[str, Any],
    enabled: bool = True,
) -> dict[str, Any]:
    now = _utc()
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO risk_rules(scope, scope_key, name, enabled, payload, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(scope, scope_key, name) DO UPDATE SET
              enabled=excluded.enabled,
              payload=excluded.payload,
              updated_at=excluded.updated_at
            """,
            (
                scope,
                scope_key,
                name,
                1 if enabled else 0,
                json.dumps(payload),
                now,
                now,
            ),
        )
    return {"ok": True}


def learn_summary() -> dict[str, Any]:
    with db_session() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM learn_samples").fetchone()["n"]
        by_label = rows_to_dicts(
            conn.execute(
                "SELECT label, COUNT(*) AS n FROM learn_samples GROUP BY label"
            ).fetchall()
        )
        clustered = conn.execute(
            "SELECT COUNT(*) AS n FROM learn_samples WHERE cluster_id IS NOT NULL"
        ).fetchone()["n"]
        rules_n = conn.execute("SELECT COUNT(*) AS n FROM risk_rules").fetchone()["n"]
    return {
        "samples": n,
        "clustered": clustered,
        "by_label": {r["label"]: r["n"] for r in by_label},
        "rules": rules_n,
        "latest_clusters": list_clusters(),
    }
