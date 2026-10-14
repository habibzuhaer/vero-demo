"""Независимый supervisor VERO: циклы агентов + launch UI на :8901.

Не делит память с server.py — управляет через HTTP и subprocess.
Запуск: ./start.sh guard | open
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    CACHE_DIR,
    HOST,
    PORT,
    SUPERVISOR_HOST,
    SUPERVISOR_PORT,
    WEB_DIR,
    ensure_dirs,
)

API = f"http://{HOST}:{PORT}"
STATE_PATH = CACHE_DIR / "supervisor_state.json"
SERVER_PID_PATH = CACHE_DIR / "vero_server.pid"
PREFETCH_STATE_PATH = CACHE_DIR / "prefetch_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _http_get(path: str, timeout: float = 4.0) -> tuple[bool, Any]:
    try:
        r = httpx.get(f"{API}{path}", timeout=timeout)
        if r.status_code >= 400:
            return False, {"status_code": r.status_code, "body": r.text[:200]}
        return True, r.json()
    except Exception as e:
        return False, {"error": str(e)[:200]}


def _http_post(path: str, body: dict | None = None, timeout: float = 8.0) -> tuple[bool, Any]:
    try:
        r = httpx.post(f"{API}{path}", json=body or {}, timeout=timeout)
        if r.status_code >= 400:
            return False, {"status_code": r.status_code, "body": r.text[:300]}
        return True, r.json()
    except Exception as e:
        return False, {"error": str(e)[:200]}


@dataclass
class AgentTick:
    name: str
    ok: bool
    message: str
    at: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LoopAgent:
    name: str = "base"
    interval_sec: float = 20.0

    def tick(self) -> AgentTick:
        raise NotImplementedError


class ServerLoopAgent(LoopAgent):
    """Держит web-server на :8900. Если мёртв — поднимает subprocess."""

    name = "server"
    interval_sec = 12.0

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._last_start = 0.0
        self._starts = 0

    def _alive(self) -> bool:
        ok, _ = _http_get("/api/health", timeout=2.5)
        return ok

    def _spawn(self) -> str:
        now = time.time()
        if now - self._last_start < 25:
            return "cooldown — wait before re-spawn"
        if self._proc and self._proc.poll() is None:
            return f"spawn already running pid={self._proc.pid}"
        ensure_dirs()
        log = CACHE_DIR / "vero_server.log"
        log_f = open(log, "a", encoding="utf-8")
        self._proc = subprocess.Popen(
            [str(ROOT / "start.sh"), "server"],
            cwd=str(ROOT),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "VERO_HOST": HOST, "VERO_PORT": str(PORT)},
        )
        self._last_start = now
        self._starts += 1
        SERVER_PID_PATH.write_text(str(self._proc.pid), encoding="utf-8")
        return f"spawned pid={self._proc.pid} log={log.name}"

    def tick(self) -> AgentTick:
        if self._alive():
            return AgentTick(
                self.name,
                True,
                f"up {API}",
                _now(),
                {"starts": self._starts, "pid": getattr(self._proc, "pid", None)},
            )
        msg = self._spawn()
        # короткая проверка после spawn
        time.sleep(1.5)
        ok = self._alive()
        return AgentTick(
            self.name,
            ok,
            msg if ok else f"down — {msg}",
            _now(),
            {"starts": self._starts},
        )


class PrefetchLoopAgent(LoopAgent):
    """Resume прерванного prefetch (state=running), не трогает ручной stop."""

    name = "prefetch"
    interval_sec = 25.0

    def tick(self) -> AgentTick:
        ok, st = _http_get("/api/prefetch/status", timeout=5.0)
        if not ok:
            return AgentTick(self.name, False, "server unreachable", _now(), {"raw": st})

        status = (st.get("status") or "idle").lower()
        details = {
            "status": status,
            "done": st.get("done"),
            "total": st.get("total"),
            "pct": st.get("pct"),
            "skipped": st.get("skipped"),
            "current": st.get("current"),
        }

        if status in ("running", "stopping"):
            return AgentTick(self.name, True, f"active {st.get('done')}/{st.get('total')}", _now(), details)

        saved = None
        try:
            if PREFETCH_STATE_PATH.exists():
                saved = json.loads(PREFETCH_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            saved = None

        if saved and saved.get("status") in ("running", "stopping"):
            uni = saved.get("universe") or "full"
            if uni == "custom":
                return AgentTick(self.name, True, "custom — no auto-resume", _now(), details)
            pok, body = _http_post(
                "/api/prefetch/start",
                {
                    "universe": uni,
                    "timeframes": saved.get("timeframes") or ["1h", "4h", "1d"],
                    "candle_limit": int(saved.get("target_bars") or 1600),
                    "force": False,
                    "resume": True,
                },
            )
            details["resume"] = body
            return AgentTick(
                self.name,
                pok,
                "auto-resume" if pok else f"resume failed: {body}",
                _now(),
                details,
            )

        return AgentTick(self.name, True, f"idle ({status})", _now(), details)


class BacktestGuardAgent(LoopAgent):
    """Не поднимает manual paused. Только наблюдает."""

    name = "backtest"
    interval_sec = 30.0

    def tick(self) -> AgentTick:
        ok, st = _http_get("/api/run/status", timeout=4.0)
        if not ok:
            return AgentTick(self.name, False, "server unreachable", _now(), {"raw": st})
        status = (st.get("status") or "idle").lower()
        msg = st.get("message") or ""
        # если вдруг running после рестарта при user-pause — stop
        if status == "running" and "prefetch only" in msg.lower():
            _http_post("/api/run/stop", {})
            return AgentTick(self.name, True, "stopped stray run (prefetch only)", _now(), st)
        return AgentTick(
            self.name,
            True,
            f"{status}",
            _now(),
            {
                "status": status,
                "done": st.get("done"),
                "total": st.get("total"),
                "message": msg[:120],
            },
        )


class StoreLoopAgent(LoopAgent):
    name = "store"
    interval_sec = 60.0

    def tick(self) -> AgentTick:
        ok, st = _http_get("/api/vero/store", timeout=8.0)
        if not ok:
            # fallback: локальный импорт без сервера
            try:
                from engine.vero_store import store_stats

                st = store_stats()
                ok = True
            except Exception as e:
                return AgentTick(self.name, False, str(e)[:160], _now())
        syms = st.get("symbols") or 0
        ready = st.get("ready_symbols_by_tf") or {}
        return AgentTick(
            self.name,
            True,
            f"{syms} sym · {st.get('size_mb')}MB",
            _now(),
            {"symbols": syms, "ready": ready, "rows": st.get("rows")},
        )


class ModulesLoopAgent(LoopAgent):
    """Лёгкий preflight модулей (без тяжёлого backtest_pipeline каждый тик)."""

    name = "modules"
    interval_sec = 120.0

    def tick(self) -> AgentTick:
        from agents.control import (
            CatalogAgent,
            ConfigAgent,
            DbAgent,
            IndicatorsAgent,
            WebAgent,
        )

        # быстрые агенты каждый цикл
        quick = [ConfigAgent(), DbAgent(), WebAgent(), CatalogAgent(), IndicatorsAgent()]
        results = [a.check() for a in quick]
        failed = [r for r in results if not r.ok]
        return AgentTick(
            self.name,
            not failed,
            "ok" if not failed else f"fail {[r.module for r in failed]}",
            _now(),
            {"checks": [r.to_dict() for r in results]},
        )


class Supervisor:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._agents: list[LoopAgent] = [
            ServerLoopAgent(),
            PrefetchLoopAgent(),
            BacktestGuardAgent(),
            StoreLoopAgent(),
            ModulesLoopAgent(),
        ]
        self._last: dict[str, dict[str, Any]] = {}
        self._started_at = _now()
        self._threads: list[threading.Thread] = []

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            agents = dict(self._last)
        return {
            "ok": all(a.get("ok", False) for a in agents.values()) if agents else False,
            "supervisor": {
                "host": SUPERVISOR_HOST,
                "port": SUPERVISOR_PORT,
                "started_at": self._started_at,
                "api": API,
                "ui": f"http://{HOST}:{PORT}/",
                "launch": f"http://{SUPERVISOR_HOST}:{SUPERVISOR_PORT}/",
            },
            "agents": agents,
            "updated_at": _now(),
        }

    def _set(self, tick: AgentTick) -> None:
        with self._lock:
            self._last[tick.name] = tick.to_dict()
        try:
            ensure_dirs()
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            STATE_PATH.write_text(
                json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def _loop(self, agent: LoopAgent) -> None:
        # первый тик сразу
        while not self._stop.is_set():
            try:
                tick = agent.tick()
            except Exception as e:
                tick = AgentTick(agent.name, False, f"exception: {e}"[:200], _now())
            self._set(tick)
            self._stop.wait(agent.interval_sec)

    def start_loops(self) -> None:
        for agent in self._agents:
            t = threading.Thread(
                target=self._loop,
                args=(agent,),
                daemon=True,
                name=f"vero-agent-{agent.name}",
            )
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()


SUPER = Supervisor()
app = FastAPI(title="VERO Supervisor", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _startup() -> None:
    ensure_dirs()
    SUPER.start_loops()


@app.on_event("shutdown")
def _shutdown() -> None:
    SUPER.stop()


@app.get("/api/status")
def api_status() -> dict[str, Any]:
    return SUPER.snapshot()


@app.post("/api/server/ensure")
def api_server_ensure() -> dict[str, Any]:
    agent = next(a for a in SUPER._agents if a.name == "server")
    tick = agent.tick()
    SUPER._set(tick)
    return tick.to_dict()


@app.post("/api/prefetch/start")
def api_pf_start() -> dict[str, Any]:
    ok, body = _http_post(
        "/api/prefetch/start",
        {
            "universe": "full",
            "timeframes": ["1h", "4h", "1d"],
            "candle_limit": 1600,
            "force": False,
            "resume": True,
        },
    )
    if not ok:
        raise HTTPException(502, str(body))
    return body if isinstance(body, dict) else {"raw": body}


@app.post("/api/prefetch/stop")
def api_pf_stop() -> dict[str, Any]:
    ok, body = _http_post("/api/prefetch/stop", {})
    if not ok:
        raise HTTPException(502, str(body))
    return body if isinstance(body, dict) else {"raw": body}


@app.post("/api/open-ui")
def api_open_ui() -> dict[str, Any]:
    url = f"http://{HOST}:{PORT}/"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    return {"ok": True, "url": url}


@app.get("/")
def launch_page() -> FileResponse:
    path = WEB_DIR / "launch.html"
    if not path.exists():
        return HTMLResponse("<h1>launch.html missing</h1>", status_code=404)
    return FileResponse(path)


def main() -> None:
    ensure_dirs()
    url = f"http://{SUPERVISOR_HOST}:{SUPERVISOR_PORT}/"
    print(f"VERO supervisor → {url}", flush=True)
    print(f"VERO UI         → http://{HOST}:{PORT}/", flush=True)
    uvicorn.run(
        app,
        host=SUPERVISOR_HOST,
        port=SUPERVISOR_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
