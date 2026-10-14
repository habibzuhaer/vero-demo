"""Bootstrap: копирует/линкует данные в VERO/data — один раз, автономно."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from config import (
    BOOTSTRAP_MTB_ROOT,
    CANDLES_DB,
    CATALOG_DIR,
    CANDLES_DIR,
    XTB_CACHE,
    XTB_QUALIFIED,
    XTB_TOP,
    ensure_dirs,
)


def autonomy_status() -> dict[str, Any]:
    ensure_dirs()
    missing: list[str] = []
    if not XTB_CACHE.exists():
        missing.append("catalog")
    if not CANDLES_DB.exists():
        missing.append("candles")
    ready = not missing
    return {
        "autonomous": ready,
        "ready": ready,
        "missing": missing,
        "paths": {
            "root": str(Path(__file__).resolve().parent),
            "catalog": str(XTB_CACHE),
            "candles": str(CANDLES_DB),
            "catalog_exists": XTB_CACHE.exists(),
            "candles_exists": CANDLES_DB.exists(),
            "candles_size_gb": round(CANDLES_DB.stat().st_size / 1e9, 2)
            if CANDLES_DB.exists()
            else 0,
        },
        "bootstrap_source": str(BOOTSTRAP_MTB_ROOT),
    }


def _copy_if_missing(src: Path, dst: Path) -> bool:
    if dst.exists():
        return False
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _link_if_missing(src: Path, dst: Path) -> bool:
    if dst.exists() or dst.is_symlink():
        return False
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.symlink_to(src.resolve())
    return True


def _looks_sqlite(path: Path) -> bool:
    try:
        if not path.exists() or path.stat().st_size < 64:
            return False
        with path.open("rb") as f:
            return f.read(16).startswith(b"SQLite format 3")
    except Exception:
        return False


def repair_candles_link(*, force_copy: bool = False) -> list[str]:
    """Windows .lnk / битый файл → symlink (или копия) на MTB trading_bot.db."""
    actions: list[str] = []
    src = BOOTSTRAP_MTB_ROOT / "data" / "trading_bot.db"
    dst = CANDLES_DB
    CANDLES_DIR.mkdir(parents=True, exist_ok=True)

    if dst.is_symlink() and dst.exists() and _looks_sqlite(dst):
        actions.append("candles symlink ok")
        return actions
    if (not dst.is_symlink()) and _looks_sqlite(dst):
        actions.append("candles sqlite ok")
        return actions

    if dst.exists() or dst.is_symlink():
        try:
            dst.unlink()
            actions.append("removed broken candles file")
        except OSError as e:
            actions.append(f"cannot remove broken candles: {e}")
            return actions

    if not src.exists():
        actions.append("candles source missing")
        return actions

    copy_db = force_copy or os.environ.get(
        "VERO_BOOTSTRAP_COPY", "0"
    ).strip().lower() in ("1", "true", "yes")
    if copy_db:
        shutil.copy2(src, dst)
        actions.append(f"copied candles → {dst.name}")
        return actions
    dst.symlink_to(src.resolve())
    actions.append(f"symlink candles → {src}")
    return actions


def bootstrap(*, force_copy_candles: bool = False) -> dict[str, Any]:
    """
    Подготовка автономных данных в data/:
    - catalog JSON → копия
    - candles DB → symlink (или copy если VERO_BOOTSTRAP_COPY=1)
    """
    ensure_dirs()
    src_root = BOOTSTRAP_MTB_ROOT
    src_data = src_root / "data"
    actions: list[str] = []

    pairs = [
        (src_data / "xtb_symbols_cache.json", XTB_CACHE),
        (src_data / "xtb_qualified_latest.json", XTB_QUALIFIED),
        (src_data / "xtb_top_pairs.json", XTB_TOP),
    ]
    for src, dst in pairs:
        if _copy_if_missing(src, dst):
            actions.append(f"copied {dst.name}")
        elif dst.exists():
            actions.append(f"ok {dst.name}")

    actions.extend(repair_candles_link(force_copy=force_copy_candles))

    st = autonomy_status()
    st["actions"] = actions
    st["ok"] = st["ready"]
    return st


if __name__ == "__main__":
    import json
    import sys

    out = bootstrap()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    sys.exit(0 if out.get("ok") else 1)
