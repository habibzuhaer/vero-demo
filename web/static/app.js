/* VERO panel */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const STRATS = [
  "rsi_mean_reversion",
  "macd_crossover",
  "bb_squeeze",
  "stoch_mean_reversion",
  "ema_crossover",
  "cci_mean_reversion",
  "willr_mean_reversion",
];

let lastPausedRunId = null;
let pollTimer = null;
let metaCache = null;
let resultsState = { sort: "oos_return_pct", order: "desc" };
let resultsLiveTimer = null;

const SETTINGS_KEY = "vero_bt_settings";

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (raw) return JSON.parse(raw);
  } catch (_) {}
  return { candle_limit: 1600, candle_max: false };
}

function saveSettings(s) {
  localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
  updateCandleUi(s);
}

function getCandlePayload() {
  const s = loadSettings();
  if (s.candle_max) return { candle_limit: 8000, candle_max: true };
  return { candle_limit: s.candle_limit || 1600, candle_max: false };
}

function formatCandleLabel(s) {
  if (s.candle_max) return "MAX (8000 cap)";
  return String(s.candle_limit || 1600);
}

function updateOfflineChip(offline) {
  const el = $("#offline-mode-chip");
  if (!el || !offline) return;
  if (offline.offline) {
    el.className = "chip offline on";
    el.textContent = `🛡 ${offline.label_uk} · ${offline.label_en}`;
  } else {
    el.className = "chip offline warn";
    el.textContent = `🌐 ${offline.label_uk} · ${offline.label_en}`;
  }
}

function updateCandleUi(s) {
  const lbl = formatCandleLabel(s);
  const chip = $("#candle-settings-chip");
  if (chip) chip.textContent = `candles ${lbl}`;
  const val = $("#settings-candle-val");
  if (val) val.textContent = lbl;
  const range = $("#settings-candle-range");
  const rangeLbl = $("#settings-candle-range-label");
  if (range && !s.candle_max) {
    range.value = String(s.candle_limit || 1000);
    if (rangeLbl) rangeLbl.textContent = range.value;
  }
  $$(".candle-preset").forEach((btn) => {
    const isMax = btn.dataset.limit === "max" && s.candle_max;
    const isNum =
      btn.dataset.limit !== "max" &&
      Number(btn.dataset.limit) === Number(s.candle_limit) &&
      !s.candle_max;
    btn.classList.toggle("selected", isMax || isNum);
  });
  const formLim = $('input[name="candle_limit"]');
  if (formLim && !s.candle_max) formLim.value = s.candle_limit || 1000;
}

function applySettingsMeta(m) {
  metaCache = m;
  updateOfflineChip(m.offline || {});
  const c = m.candles || {};
  const note = $("#settings-candle-note");
  if (note) {
    note.textContent =
      `Діапазон UI: ${c.min ?? 1000}–${c.max ?? 8000}. ` +
      `У БД max ~${c.local_max_bars ?? "?"} bars/ticker. ` +
      `MAX = cap ${c.max ?? 8000}. / ` +
      `Local DB max ~${c.local_max_bars ?? "?"} bars.`;
  }
  $("#settings-yahoo") &&
    ($("#settings-yahoo").textContent = m.offline?.yahoo_fallback ? "on" : "off");
  $("#settings-local") &&
    ($("#settings-local").textContent = m.offline?.require_local ? "yes" : "no");
  $("#settings-loc-max") &&
    ($("#settings-loc-max").textContent = c.local_max_bars ?? "—");
  const ob = $("#settings-offline-info");
  if (ob && m.offline) {
    ob.className = m.offline.offline ? "offline-banner on" : "offline-banner warn";
    ob.innerHTML =
      `<strong>Режим: ${m.offline.label_uk}</strong><br>` +
      `<span class="guide-en">Mode: ${m.offline.label_en}</span>`;
  }
  updateCandleUi(loadSettings());
}

function setupSettings() {
  updateCandleUi(loadSettings());

  $$(".candle-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.limit === "max") {
        saveSettings({ candle_limit: 8000, candle_max: true });
      } else {
        saveSettings({ candle_limit: Number(btn.dataset.limit), candle_max: false });
      }
    });
  });

  const range = $("#settings-candle-range");
  if (range) {
    range.addEventListener("input", () => {
      const v = Number(range.value);
      $("#settings-candle-range-label").textContent = String(v);
      saveSettings({ candle_limit: v, candle_max: false });
    });
  }
}

const STATUS_I18N = {
  idle: {
    uk: "Очікування",
    en: "Idle",
    hint_uk: "Немає активного прогону. Обери тривалість і натисни кнопку запуску.",
    hint_en: "No active run. Pick duration and press launch.",
  },
  running: {
    uk: "Працює",
    en: "Running",
    hint_uk: "Бэктест виконується зараз.",
    hint_en: "Backtest is running now.",
  },
  paused: {
    uk: "На паузі",
    en: "Paused",
    hint_uk: "Зупинено вручну. Натисни «Продовжити / Resume».",
    hint_en: "Stopped manually. Press Resume.",
  },
  daily_pause: {
    uk: "Ліміт сесії",
    en: "Session limit",
    hint_uk: "Досягнуто ліміт годин. Продовжити після паузи або нового запуску.",
    hint_en: "Session hour limit reached. Resume after pause or relaunch.",
  },
  done: {
    uk: "Завершено",
    en: "Completed",
    hint_uk: "Прогон повністю завершений.",
    hint_en: "Run finished successfully.",
  },
  error: {
    uk: "Помилка",
    en: "Error",
    hint_uk: "Прогон завершився з помилкою.",
    hint_en: "Run ended with an error.",
  },
};

function fmtTs(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
    return d.toLocaleString("uk-UA", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch (_) {
    return iso;
  }
}

function dash(v) {
  return v === null || v === undefined || v === "" ? "—" : v;
}

const STRAT_SHORT = {
  rsi_mean_reversion: "RSI",
  macd_crossover: "MACD",
  bb_squeeze: "BB",
  stoch_mean_reversion: "Stoch",
  ema_crossover: "EMA",
  cci_mean_reversion: "CCI",
  willr_mean_reversion: "WillR",
};

function shortStrat(id) {
  return STRAT_SHORT[id] || String(id || "—").replace(/_mean_reversion|_crossover|_squeeze/g, "");
}

function fmt(n, d = 2) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  return Number(n).toFixed(d);
}

/** Компактный %: 12.3 · 1.4k · −0.8 */
function fmtPct(n, d = 1) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const x = Number(n);
  const abs = Math.abs(x);
  if (abs >= 1000) return `${(x / 1000).toFixed(1)}k`;
  if (abs >= 100) return x.toFixed(0);
  return x.toFixed(d);
}

/** PF: 999 в метриках = ∞ (нет убытков) */
function fmtPf(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const x = Number(n);
  if (x >= 999) return "∞";
  if (x >= 100) return x.toFixed(0);
  return x.toFixed(2);
}

function fmtCount(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "—";
  const x = Math.round(Number(n));
  if (Math.abs(x) >= 1e6) return `${(x / 1e6).toFixed(1)}M`;
  if (Math.abs(x) >= 10000) return `${(x / 1e3).toFixed(1)}k`;
  return x.toLocaleString("en-US");
}

function clsRet(v) {
  if (v === null || v === undefined) return "";
  return Number(v) >= 0 ? "pos" : "neg";
}

function escAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function oosN(r) {
  const n = r.oos_closed ?? r.oos_trades ?? null;
  return n == null ? null : Number(n);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.message || res.statusText);
  return data;
}

function setupTabs() {
  $$(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".tab").forEach((b) => b.classList.remove("active"));
      $$(".panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "results") {
        loadResults();
        ensureResultsLive();
      } else {
        stopResultsLive();
      }
      if (btn.dataset.tab === "top") {
        loadAutoTop();
        loadUserTop();
        refreshTruthStatus();
      }
      if (btn.dataset.tab === "history") loadRuns();
      if (btn.dataset.tab === "learn") loadLearn();
      if (btn.dataset.tab === "settings" && metaCache) applySettingsMeta(metaCache);
      if (btn.dataset.tab === "signals") loadSignalsTab();
    });
  });
}

function chartUrl(path) {
  if (!path) return null;
  const name = String(path).split("/").pop();
  return name ? `/api/charts/${encodeURIComponent(name)}` : null;
}

function eventBadge(et) {
  if (et === "BUY") return `<span class="sig-badge buy">BUY</span>`;
  if (et === "SELL") return `<span class="sig-badge sell">SELL</span>`;
  return et || "—";
}

async function loadTelegramStatus() {
  const tg = await api("/api/telegram/status");
  const banner = $("#tg-status-banner");
  if (!banner) return tg;
  if (tg.configured) {
    banner.className = "tg-banner ok";
    banner.innerHTML =
      `<strong>✓ Telegram ready</strong><br>` +
      `<span class="guide-en">Auto-scan ${tg.scan_enabled ? "ON" : "OFF"} · every ${tg.scan_interval_sec}s</span>`;
  } else if (tg.has_token && !tg.has_chat_id) {
    banner.className = "tg-banner warn";
    banner.innerHTML =
      `<strong>⚠ Token OK · chat_id missing</strong><br>` +
      `<span class="guide-en">Write /start to bot, then Discover chat_id</span>`;
  } else {
    banner.className = "tg-banner err";
    banner.innerHTML =
      `<strong>✗ Not configured</strong><br>` +
      `<span class="guide-en">Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env</span>`;
  }
  return tg;
}

async function loadPaperStatus() {
  const p = await api("/api/paper/status");
  const cash = Number(p.cash ?? 0);
  const init = Number(p.initial_cash ?? 0);
  const pnl = init ? ((cash / init - 1) * 100) : 0;
  const sum = $("#paper-summary");
  if (sum) {
    sum.innerHTML = [
      `cash <b class="mono">${fmt(cash, 2)}</b>`,
      `initial <b class="mono">${fmt(init, 2)}</b>`,
      `open <b>${p.open_count ?? 0}</b>`,
      `PnL <b class="${clsRet(pnl)}">${fmt(pnl, 2)}%</b>`,
    ].join(" · ");
  }

  const ptb = $("#paper-positions-table tbody");
  const pos = p.positions || [];
  ptb.innerHTML =
    pos.length === 0
      ? `<tr><td colspan="6">Немає відкритих / none open</td></tr>`
      : pos
          .map(
            (r) => `<tr>
          <td>${r.symbol}</td><td>${r.timeframe}</td><td>${r.strategy}</td>
          <td class="mono">${fmt(r.entry_price, 4)}</td>
          <td class="mono">${fmt(r.stop_price, 4)}</td>
          <td class="mono">${fmt(r.qty, 4)}</td>
        </tr>`
          )
          .join("");

  const ttb = $("#paper-trades-table tbody");
  const trades = p.recent_trades || [];
  ttb.innerHTML =
    trades.length === 0
      ? `<tr><td colspan="7">Ще немає угод / no trades yet</td></tr>`
      : trades
          .map(
            (r) => `<tr>
          <td class="mono">${fmtTs(r.created_at)}</td>
          <td>${eventBadge(r.side)}</td>
          <td>${r.symbol}</td><td>${r.timeframe}</td>
          <td class="mono">${fmt(r.price, 4)}</td>
          <td class="${clsRet(r.pnl_pct)}">${r.pnl_pct != null ? fmt(r.pnl_pct) : "—"}</td>
          <td>${r.reason || "—"}</td>
        </tr>`
          )
          .join("");
  return p;
}

async function loadWatchlist() {
  const data = await api("/api/signals/watchlist");
  const items = data.items || [];
  $("#watch-count").textContent = `${items.length} pairs`;
  const tb = $("#watchlist-table tbody");
  tb.innerHTML =
    items.length === 0
      ? `<tr><td colspan="5">Пусто — додай у My Top або дочекайся бэктесту</td></tr>`
      : items
          .slice(0, 80)
          .map(
            (r) => `<tr>
          <td>${r.symbol}</td><td>${r.timeframe}</td><td>${r.strategy}</td>
          <td>${r.source || "—"}</td>
          <td class="${clsRet(r.oos_return_pct)}">${fmt(r.oos_return_pct)}</td>
        </tr>`
          )
          .join("");
}

async function loadRecentSignals() {
  const data = await api("/api/signals/recent?limit=40");
  const rows = data.signals || [];
  const tb = $("#signals-table tbody");
  tb.innerHTML =
    rows.length === 0
      ? `<tr><td colspan="9">Сигналів ще немає — Scan now або дочекайся авто-скану</td></tr>`
      : rows
          .map((r) => {
            const cu = chartUrl(r.chart_path);
            const tg = r.telegram_sent
              ? "✓"
              : r.telegram_error
                ? `<span class="neg" title="${String(r.telegram_error).replace(/"/g, "&quot;")}">✗</span>`
                : "—";
            return `<tr>
          <td class="mono">${fmtTs(r.created_at)}</td>
          <td>${eventBadge(r.event_type)}</td>
          <td>${r.symbol}</td><td>${r.timeframe}</td><td>${r.strategy}</td>
          <td class="mono">${fmt(r.price, 4)}</td>
          <td class="mono">${fmt(r.stop_price, 4)}</td>
          <td>${tg}</td>
          <td>${
            cu
              ? `<a href="${cu}" target="_blank" rel="noopener"><img class="sig-thumb" src="${cu}" alt="chart" loading="lazy" /></a>`
              : "—"
          }</td>
        </tr>`;
          })
          .join("");
}

async function loadSignalsTab() {
  try {
    await Promise.all([
      loadTelegramStatus(),
      loadPaperStatus(),
      loadWatchlist(),
      loadRecentSignals(),
    ]);
  } catch (err) {
    alert(err.message);
  }
}

function setupSignals() {
  $("#btn-signals-refresh")?.addEventListener("click", loadSignalsTab);
  $("#btn-signal-scan")?.addEventListener("click", async () => {
    const btn = $("#btn-signal-scan");
    btn.disabled = true;
    btn.textContent = "Scanning…";
    try {
      const out = await api("/api/signals/scan", {
        method: "POST",
        body: JSON.stringify({ send_telegram: true }),
      });
      alert(
        `Watch: ${out.watch_count} · fired: ${out.fired} · stops: ${out.stops ?? 0} · skipped: ${out.skipped}`
      );
      await loadSignalsTab();
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Scan now";
    }
  });
  $("#btn-tg-discover")?.addEventListener("click", async () => {
    const out = $("#tg-discover-out");
    out.textContent = "Querying getUpdates…";
    try {
      const data = await api("/api/telegram/discover");
      if (!data.ok) {
        out.innerHTML = `<span class="neg">${data.error || "failed"}</span>`;
        return;
      }
      const chats = data.chats || [];
      if (!chats.length) {
        out.innerHTML =
          `<span class="warn">Чатів не знайдено.</span> Напиши боту <strong>/start</strong> і спробуй знову.`;
        return;
      }
      out.innerHTML = chats
        .map(
          (c) =>
            `<div class="tg-chat-row">` +
            `<code class="mono">${c.chat_id}</code> · ${c.type || "?"} · ${c.title || "—"} ` +
            `<button type="button" class="linkish" data-copy="${c.chat_id}">copy</button>` +
            `</div>`
        )
        .join("");
      out.querySelectorAll("[data-copy]").forEach((btn) => {
        btn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(btn.dataset.copy);
            btn.textContent = "copied!";
            setTimeout(() => {
              btn.textContent = "copy";
            }, 1500);
          } catch (_) {
            prompt("chat_id:", btn.dataset.copy);
          }
        });
      });
    } catch (err) {
      out.innerHTML = `<span class="neg">${err.message}</span>`;
    }
  });
}

function fillStrategies() {
  const box = $("#strat-box");
  const f = $("#f-strat");
  box.innerHTML = STRATS.map(
    (s) =>
      `<label class="check"><input type="checkbox" name="st" value="${s}" checked /> ${s}</label>`
  ).join("");
  f.innerHTML =
    `<option value="">all strategies</option>` +
    STRATS.map((s) => `<option value="${s}">${s}</option>`).join("");
}

async function loadMeta() {
  const m = await api("/api/meta");
  const c = m.catalog || {};
  const lc = m.local_candles || {};
  const vs = m.vero_store || {};
  $("#catalog-stats").textContent = [
    `full ${c.full ?? "—"}`,
    `q ${c.qualified ?? "—"}`,
    `loc ${lc.symbols_30m ?? 0}/${lc.symbols_1h ?? 0}/${lc.symbols_4h ?? 0}`,
    `vero ${vs.symbols ?? 0}sym`,
  ].join(" · ");
  updateAutonomy(m.autonomy || {});
  applySettingsMeta(m);
  updatePrefetchUi(m.prefetch || {});
  updateVeroStoreUi(vs);
  ensurePrefetchPoll(m.prefetch || {});
}

function updateVeroStoreUi(vs) {
  const el = $("#vero-store-info");
  if (!el) return;
  if (!vs || !vs.available) {
    el.textContent = "empty";
    return;
  }
  const ready = vs.ready_symbols_by_tf || {};
  const readyTxt = Object.keys(ready)
    .map((k) => `${k}:${ready[k]}`)
    .join(" ");
  el.textContent = `${vs.symbols} sym · ${vs.rows} bars · ${vs.size_mb}MB · ${readyTxt || "—"}`;
}

function updatePrefetchUi(st) {
  const fill = $("#prefetch-bar-fill");
  const status = $("#prefetch-status");
  const cur = st.current ? ` · ${st.current}` : "";
  const resumeBit = st.resumed ? " · resume" : "";
  const line =
    `${st.status || "idle"} ${st.pct ?? 0}% · ` +
    `done ${st.done ?? 0}/${st.total ?? 0} · ok ${st.ok ?? 0} · fail ${st.failed ?? 0}` +
    ` · skip ${st.skipped ?? 0}` +
    resumeBit +
    cur +
    (st.message ? ` · ${st.message}` : "");
  if (fill) fill.style.width = `${Math.min(100, Number(st.pct) || 0)}%`;
  if (status) status.textContent = line;

  const strip = $("#pf-progress-strip");
  if (strip) {
    const raw = (st.status || "idle").toLowerCase();
    const klass =
      raw === "running" || raw === "stopping"
        ? "running"
        : raw === "done"
          ? "done"
          : raw === "error" || raw === "stopped"
            ? "error"
            : "idle";
    strip.className = `bt-progress-strip pf-strip ${klass}`;
    const pst = $("#pf-prog-status");
    const pcnt = $("#pf-prog-count");
    const ppct = $("#pf-prog-pct");
    const pbar = $("#pf-prog-bar");
    const pmsg = $("#pf-prog-msg");
    if (pst) pst.textContent = (st.status || "idle").toUpperCase();
    if (pcnt) pcnt.textContent = `${st.done ?? 0} / ${st.total ?? 0}`;
    if (ppct) ppct.textContent = `${st.pct ?? 0}%`;
    if (pbar) pbar.style.width = `${Math.min(100, Number(st.pct) || 0)}%`;
    if (pmsg) {
      const store = st.store || {};
      const storeBit = store.symbols
        ? ` · store ${store.symbols} sym / ${store.size_mb ?? "?"}MB`
        : "";
      pmsg.textContent = line + storeBit;
    }
  }
}

let prefetchPoll = null;
function ensurePrefetchPoll(st) {
  const running = st && (st.status === "running" || st.status === "stopping");
  if (running && !prefetchPoll) {
    prefetchPoll = setInterval(async () => {
      try {
        const j = await api("/api/prefetch/status");
        updatePrefetchUi(j);
        updateVeroStoreUi(j.store || {});
        if (j.status !== "running" && j.status !== "stopping") {
          clearInterval(prefetchPoll);
          prefetchPoll = null;
          loadMeta().catch(() => {});
          refreshTruthStatus().catch(() => {});
        }
      } catch (_) {}
    }, 1500);
  }
  if (!running && prefetchPoll) {
    clearInterval(prefetchPoll);
    prefetchPoll = null;
  }
}

async function startPrefetch(universe) {
  const candle = getCandlePayload();
  const body = {
    universe,
    timeframes: ["1h", "4h", "1d"],
    candle_limit: candle.candle_max ? 8000 : candle.candle_limit || 1600,
    force: false,
    resume: true,
  };
  const j = await api("/api/prefetch/start", {
    method: "POST",
    body: JSON.stringify(body),
  });
  updatePrefetchUi(j);
  ensurePrefetchPoll(j);
  return j;
}

function setupPrefetch() {
  const bind = (id, fn) => $(id)?.addEventListener("click", fn);
  const startSeed = async () => {
    try {
      await startPrefetch("seed");
    } catch (err) {
      alert(err.message);
    }
  };
  const startFull = async () => {
    if (!confirm("Full catalog (~9700×3 TF). Days of download. Start?")) return;
    try {
      await startPrefetch("full");
    } catch (err) {
      alert(err.message);
    }
  };
  const stopPf = async () => {
    try {
      const j = await api("/api/prefetch/stop", { method: "POST", body: "{}" });
      updatePrefetchUi(j);
    } catch (err) {
      alert(err.message);
    }
  };
  bind("#prefetch-seed-btn", startSeed);
  bind("#home-prefetch-seed-btn", startSeed);
  bind("#prefetch-full-btn", startFull);
  bind("#home-prefetch-full-btn", startFull);
  bind("#prefetch-stop-btn", stopPf);
  bind("#home-prefetch-stop-btn", stopPf);
}

async function loadChartSymbols() {
  const sel = $("#chart-symbol");
  if (!sel) return;
  const tf = $("#chart-tf")?.value || "4h";
  try {
    const j = await api(`/api/vero/symbols?timeframe=${encodeURIComponent(tf)}`);
    const syms = j.symbols || [];
    const prev = sel.value;
    sel.innerHTML = syms.length
      ? syms.map((s) => `<option value="${s}">${s}</option>`).join("")
      : `<option value="">(empty store)</option>`;
    if (prev && syms.includes(prev)) sel.value = prev;
    else if (syms.includes("WULF.US")) sel.value = "WULF.US";
  } catch (_) {
    sel.innerHTML = `<option value="">error</option>`;
  }
}

async function drawLocalChart() {
  const symbol = $("#chart-symbol")?.value;
  const timeframe = $("#chart-tf")?.value || "4h";
  const bars = Number($("#chart-bars")?.value || 160);
  const meta = $("#chart-meta");
  const img = $("#chart-img");
  if (!symbol) {
    if (meta) meta.textContent = "no symbol in local store";
    return;
  }
  if (meta) meta.textContent = "drawing…";
  try {
    const q = new URLSearchParams({
      symbol,
      timeframe,
      bars: String(bars),
    });
    const j = await api(`/api/chart/local?${q}`);
    if (meta) {
      meta.textContent = `${j.symbol} ${j.timeframe} · ${j.bars} bars · last ${j.last_close}`;
    }
    if (img && j.url) {
      img.hidden = false;
      img.src = `${j.url}?t=${Date.now()}`;
    }
  } catch (err) {
    if (meta) meta.textContent = err.message;
    if (img) img.hidden = true;
  }
}

function setupChart() {
  $("#chart-draw-btn")?.addEventListener("click", () => drawLocalChart());
  $("#chart-tf")?.addEventListener("change", async () => {
    await loadChartSymbols();
  });
  document.querySelector('.tab[data-tab="chart"]')?.addEventListener("click", () => {
    loadChartSymbols().then(() => {
      if ($("#chart-symbol")?.value) drawLocalChart();
    });
  });
}

function updateAutonomy(a) {
  const el = $("#autonomy-banner");
  if (!el) return;
  if (a.ready) {
    el.className = "chip autonomy ok";
    const sz = a.paths?.candles_size_gb;
    el.textContent = [
      "✓ data ok",
      sz ? `${sz}GB` : "",
    ]
      .filter(Boolean)
      .join(" · ");
  } else {
    el.className = "chip autonomy warn";
    el.textContent = `⚠ ${(a.missing || []).join(", ") || "?"} → Bootstrap`;
  }
}

async function loadModes() {
  const data = await api("/api/modes");
  updateAutonomy(data.autonomy || {});
  const grid = $("#modes-grid");
  const modes = data.modes || [];
  grid.innerHTML = modes
    .map((m) => {
      const actionOnly = !!m.action;
      return `<button type="button" class="mode-card${actionOnly ? " action-only" : ""}" data-mode="${m.id}">
        <div class="icon">${m.icon || "▶"}</div>
        <div class="title">${m.title}</div>
        <div class="desc">${m.desc}</div>
      </button>`;
    })
    .join("");
  grid.querySelectorAll("[data-mode]").forEach((btn) => {
    btn.addEventListener("click", () => runMode(btn.dataset.mode));
  });
}

async function runMode(modeId) {
  const candle = getCandlePayload();
  try {
    const j = await api(`/api/run/mode/${modeId}`, {
      method: "POST",
      body: JSON.stringify(candle),
    });
    if (j.action === "preflight") {
      const ok = j.ok ? "OK" : "FAIL";
      alert(`Preflight ${ok}: ${j.passed}/${j.passed + j.failed} passed`);
      return;
    }
    if (j.action === "bootstrap") {
      updateAutonomy(j);
      await loadMeta();
      await loadModes();
      alert(j.ready ? "Bootstrap готов" : `Bootstrap: ${(j.actions || []).join("; ")}`);
      return;
    }
    if (j.action === "learn") {
      alert(j.ok ? `Clusters: ${j.clusters ?? "ok"}` : j.error || "learn failed");
      loadLearn();
      return;
    }
    if (j.action === "prefetch") {
      updatePrefetchUi(j);
      ensurePrefetchPoll(j);
      alert(`Prefetch ${j.status}: ${j.done || 0}/${j.total || 0}`);
      return;
    }
    updateJob(j);
    startPoll();
  } catch (err) {
    alert(err.message);
  }
}

function updateStatusWidget(j) {
  const status = j.status || "idle";
  const run = j.run || j.last_run || null;
  const i18n = STATUS_I18N[status] || STATUS_I18N.idle;

  const badge = $("#st-badge");
  if (badge) {
    badge.className = `status-badge ${status}`;
    $("#st-uk").textContent = i18n.uk;
    $("#st-en").textContent = i18n.en;
  }

  const stale = run?.stale && !j.worker_alive;
  let hint = `${i18n.hint_uk} / ${i18n.hint_en}`;
  if (stale) {
    hint =
      "Воркер зупинений — прогон у БД «running». Натисни Resume. / " +
      "Worker stopped — DB shows running. Press Resume.";
  }
  const hintEl = $("#st-hint");
  if (hintEl) hintEl.textContent = hint;

  const runRef = $("#st-run-ref");
  if (runRef) {
    if (run) {
      runRef.textContent = `#${run.id} · ${run.tag}${stale ? " · stale" : ""}`;
    } else {
      runRef.textContent = "немає прогонів / no runs";
    }
  }

  const done = j.done ?? run?.done_jobs ?? 0;
  const total = j.total ?? run?.total_jobs ?? 0;
  const ok = j.ok ?? run?.ok_jobs ?? 0;
  const err = j.err ?? run?.err_jobs ?? 0;
  const skip = j.skipped ?? run?.skipped_jobs ?? 0;
  const pend = j.pending ?? run?.pending_jobs ?? 0;
  const pct = j.pct ?? run?.pct ?? (total ? Math.round((done / total) * 10) / 10 : 0);

  $("#st-pct-label").textContent = `${pct}%`;
  $("#st-count").textContent = `${fmtCount(done)} / ${fmtCount(total)}`;
  $("#st-bar").style.width = `${Math.min(100, pct)}%`;
  $("#st-ok").textContent = fmtCount(ok);
  $("#st-err").textContent = fmtCount(err);
  $("#st-skip").textContent = fmtCount(skip);
  $("#st-pend").textContent = fmtCount(pend);

  let current = "—";
  if (status === "running" && j.message) {
    current = j.message;
  } else if (status === "paused" || status === "daily_pause") {
    current = j.message || run?.message || status;
  } else if (status === "done") {
    current = "completed / завершено";
  } else if (status === "error") {
    current = j.message || run?.message || "error";
  } else if (run && pend > 0) {
    current = `${pend} pending / в черзі`;
  }
  $("#st-current").textContent = current;

  if (run) {
    $("#st-universe").textContent = run.universe || "—";
    $("#st-scope").textContent =
      `${run.timeframes_count ?? run.timeframes?.length ?? "?"} TF × ` +
      `${run.strategies_count ?? run.strategies?.length ?? "?"} strat`;
    $("#st-started").textContent = fmtTs(run.created_at);
    $("#st-updated").textContent = fmtTs(run.updated_at);
    $("#st-finished").textContent = fmtTs(run.finished_at);
  } else {
    ["st-universe", "st-scope", "st-started", "st-updated", "st-finished"].forEach((id) => {
      const el = $(`#${id}`);
      if (el) el.textContent = "—";
    });
  }

  const sched = j.scheduler || {};
  const mode = sched.session_mode || "—";
  const lbl = sched.session_label || "";
  const used = sched.used_hours ?? 0;
  const budget = sched.unlimited ? "∞" : (sched.budget_hours ?? "—");
  const left = sched.unlimited ? "∞" : (sched.remaining_hours ?? "—");
  $("#st-session").textContent =
    mode === "—"
      ? "—"
      : `${mode} ${lbl} · ${used}h / ${budget}h · left ${left}h`;

  let msg = j.message || run?.message || "—";
  if (status === "idle" && !run) msg = "—";
  $("#st-message").textContent = dash(msg);

  updateProgressStrip(j);
}

function updateProgressStrip(j) {
  const strip = $("#bt-progress-strip");
  if (!strip) return;

  const status = j.status || "idle";
  const run = j.run || j.last_run || null;
  const stale = !!(run?.stale && !j.worker_alive);
  const done = j.done ?? run?.done_jobs ?? 0;
  const total = j.total ?? run?.total_jobs ?? 0;
  const ok = j.ok ?? run?.ok_jobs ?? 0;
  const err = j.err ?? run?.err_jobs ?? 0;
  const skip = j.skipped ?? run?.skipped_jobs ?? 0;
  const pct = j.pct ?? run?.pct ?? (total ? Math.round((done / total) * 1000) / 10 : 0);

  strip.hidden = false;
  strip.className = `bt-progress-strip ${stale ? "stale" : status}`;

  const bars = run?.candle_limit ? `${run.candle_limit} bars` : "";
  const src = metaCache?.data_source?.yahoo_first ? "yahoo-first" : "local";
  $("#bt-prog-label").textContent = bars ? `Backtest · ${bars} · ${src}` : `Backtest · ${src}`;

  let stLabel = stale ? "STALE" : status.toUpperCase();
  if (status === "running" && j.worker_alive === false) stLabel = "STALE";
  $("#bt-prog-status").textContent = stLabel;
  $("#bt-prog-count").textContent = `${fmtCount(done)} / ${fmtCount(total)}`;
  $("#bt-prog-pct").textContent = `${pct}%`;
  $("#bt-prog-bar").style.width = `${Math.min(100, pct)}%`;

  const tag = run ? `#${run.id} · ${run.tag}` : "—";
  const cur = j.message || run?.message || "—";
  const worker = j.worker_alive ? "worker ✓" : "worker ✗";
  $("#bt-prog-msg").textContent =
    `${tag} · ${worker} · OK ${fmtCount(ok)} · ERR ${fmtCount(err)} · SKIP ${fmtCount(skip)} · ${cur}`;
  if (status === "idle" && !run) {
    $("#bt-prog-msg").textContent = "немає активного прогону / no active run";
  }
}

function updateScheduler(sched) {
  if (!sched || sched.budget_hours == null) return;
  const el = $("#scheduler-stats");
  if (!el) return;
  const mode = sched.session_mode || "daily";
  const lbl = sched.session_label || "";
  const used = sched.used_hours ?? 0;
  const budget = sched.unlimited ? "∞" : sched.budget_hours;
  const left = sched.unlimited ? "∞" : (sched.remaining_hours ?? 0);

  let modeUk = "день";
  let modeEn = "daily";
  if (mode === "fixed") {
    modeUk = `сесія ${lbl}`;
    modeEn = `session ${lbl}`;
  } else if (mode === "unlimited") {
    modeUk = "цикл ∞";
    modeEn = "cycle ∞";
  }

  el.textContent = [
    `${modeUk}/${modeEn}`,
    `${used}h/${budget}h`,
    `←${left}h`,
  ].join(" · ");
}

function updateJob(j) {
  const pill = $("#job-pill");
  const status = j.status || "idle";
  pill.textContent = status;
  pill.className = `pill ${status}`;
  updateStatusWidget(j);
  updateScheduler(j.scheduler || {});
  const prog = $("#progress");
  if (status === "running" || status === "paused" || status === "daily_pause") {
    prog.hidden = false;
    $("#prog-msg").textContent = j.message || status;
    $("#prog-count").textContent = `${j.done || 0} / ${j.total || 0}`;
    $("#prog-pct").textContent = `${j.pct || 0}%`;
    $("#prog-bar").style.width = `${j.pct || 0}%`;
  } else if (status === "done" || status === "error") {
    prog.hidden = false;
    $("#prog-msg").textContent = j.message || status;
    $("#prog-count").textContent = `${j.done || 0} / ${j.total || 0}`;
    $("#prog-pct").textContent = `${j.pct || 0}%`;
    $("#prog-bar").style.width = `${j.pct || 0}%`;
  } else {
    prog.hidden = true;
  }
  if (status === "paused" && j.run_id) lastPausedRunId = j.run_id;
}

async function launchDuration(hours) {
  const isCycle = !hours || Number(hours) <= 0;
  const label = isCycle ? "Cycle until cancel / Цикл до скасування" : `${hours}h`;

  const status = await api("/api/run/status").catch(() => ({ status: "idle" }));
  if (status.status === "running") {
    const ok = confirm(
      `Вже працює прогон. Зупинити і запустити ${label}?\n` +
        `Run in progress. Stop and launch ${label}?`
    );
    if (!ok) return;
    await api("/api/run/stop", { method: "POST" });
    await new Promise((r) => setTimeout(r, 800));
  }

  $$(".dur-btn").forEach((b) => b.classList.remove("selected"));
  const btn = $(`.dur-btn[data-hours="${isCycle ? "0" : hours}"]`);
  if (btn) btn.classList.add("selected");

  try {
    const j = await api("/api/run/launch", {
      method: "POST",
      body: JSON.stringify({
        hours: isCycle ? null : Number(hours),
        universe: "full",
        resume: false,
        ...getCandlePayload(),
      }),
    });
    updateJob(j);
    startPoll();
  } catch (err) {
    alert(err.message);
  }
}

function setupLaunch() {
  $$(".dur-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const h = btn.dataset.hours;
      launchDuration(h === "0" ? 0 : Number(h));
    });
  });
}

function startPoll() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const j = await api("/api/run/status");
      updateJob(j);
      const keep = ["running", "paused", "daily_pause"];
      if (!keep.includes(j.status)) {
        clearInterval(pollTimer);
        pollTimer = null;
        if (j.status === "done") {
          loadRuns();
          loadLearn();
        }
      }
    } catch (_) {}
  }, 1200);
}

function ensurePoll(status) {
  if (["running", "paused", "daily_pause"].includes(status)) startPoll();
}

function ensurePollFromJob(j) {
  const st = j.status || "idle";
  const run = j.run || j.last_run;
  const stale = run?.stale && !j.worker_alive;
  if (["running", "paused", "daily_pause"].includes(st) || stale) startPoll();
}

async function doStop() {
  const j = await api("/api/run/stop", { method: "POST" });
  updateJob(j.job || j);
}

async function doResume() {
  let id = lastPausedRunId;
  if (!id) {
    const runs = await api("/api/runs");
    const cand = (runs.runs || []).find(
      (r) =>
        r.status === "paused" ||
        r.status === "daily_pause" ||
        r.status === "running"
    );
    id = cand && cand.id;
  }
  if (!id) {
    alert("Немає прогону для Resume / No run to resume");
    return;
  }
  try {
    const j = await api("/api/run/resume", {
      method: "POST",
      body: JSON.stringify({ run_id: id }),
    });
    updateJob(j);
    startPoll();
  } catch (err) {
    alert(err.message);
  }
}

async function guideAction(act) {
  try {
    if (act === "stop") {
      await doStop();
      return;
    }
    if (act === "resume") {
      await doResume();
      return;
    }
    if (act === "smoke") {
      await runMode("smoke");
      startPoll();
      return;
    }
    if (act === "bootstrap" || act === "preflight") {
      await runMode(act);
      if (act === "bootstrap") {
        await loadMeta();
        await loadModes();
      }
      return;
    }
  } catch (err) {
    alert(err.message);
  }
}

function setupGuide() {
  $$(".toolbar [data-act], .guide-body [data-act]").forEach((el) => {
    el.addEventListener("click", () => guideAction(el.dataset.act));
  });
}

function setupRunForm() {
  $("#run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const tfs = $$('input[name="tf"]:checked').map((x) => x.value);
    const strats = $$('input[name="st"]:checked').map((x) => x.value);
    const slRaw = fd.get("stop_loss_pct");
    const body = {
      universe: fd.get("universe"),
      timeframes: tfs,
      strategies: strats,
      candle_limit: Number(fd.get("candle_limit")),
      cash: Number(fd.get("cash")),
      fee_rate: Number(fd.get("fee_rate")),
      stop_loss_pct: slRaw === "" || slRaw === null ? null : Number(slRaw),
      save_trades: !!fd.get("save_trades"),
      symbols: (fd.get("symbols") || "").trim() || null,
    };
    try {
      const j = await api("/api/run/start", {
        method: "POST",
        body: JSON.stringify(body),
      });
      updateJob(j);
      startPoll();
    } catch (err) {
      alert(err.message);
    }
  });

  $("#btn-stop").addEventListener("click", () => doStop());

  $("#btn-resume").addEventListener("click", () => doResume());
}

function resultsQuery() {
  const q = new URLSearchParams();
  const run = $("#f-run")?.value;
  const sym = $("#f-symbol")?.value.trim();
  const tf = $("#f-tf")?.value;
  const st = $("#f-strat")?.value;
  const oos = $("#f-oos")?.value;
  const pf = $("#f-pf")?.value;
  const wr = $("#f-wr")?.value;
  const dd = $("#f-dd")?.value;
  const n = $("#f-n")?.value;
  const limit = $("#f-limit")?.value || "300";
  if (run === "latest") q.set("latest", "true");
  else if (run) q.set("run_id", run);
  if (sym) q.set("symbol", sym);
  if (tf) q.set("timeframe", tf);
  if (st) q.set("strategy", st);
  if (oos !== "") q.set("min_oos", oos);
  if (pf !== "") q.set("min_pf", pf);
  if (wr !== "") q.set("min_wr", wr);
  if (dd !== "") q.set("max_dd", dd);
  if (n !== "") q.set("min_n", n);
  q.set("sort", resultsState.sort);
  q.set("order", resultsState.order);
  q.set("limit", limit);
  return q;
}

function markSortHeaders() {
  $$("#results-table thead th.sortable").forEach((th) => {
    th.classList.toggle("sort-asc", th.dataset.sort === resultsState.sort && resultsState.order === "asc");
    th.classList.toggle("sort-desc", th.dataset.sort === resultsState.sort && resultsState.order === "desc");
  });
}

async function fillResultsRuns() {
  const sel = $("#f-run");
  if (!sel) return;
  const keep = sel.value;
  try {
    const data = await api("/api/runs?limit=20");
    const runs = data.runs || [];
    const opts = [
      `<option value="latest">latest run</option>`,
      `<option value="">all runs</option>`,
      ...runs.map((r) => {
        const tag = r.tag || `#${r.id}`;
        const sl = r.stop_loss_pct != null ? ` SL${r.stop_loss_pct}` : "";
        return `<option value="${r.id}">#${r.id} ${tag}${sl} · ${r.status}</option>`;
      }),
    ];
    sel.innerHTML = opts.join("");
    if ([...sel.options].some((o) => o.value === keep)) sel.value = keep;
    else sel.value = "latest";
  } catch (_) {
    /* keep current */
  }
}

async function loadResults() {
  const tb = $("#results-table tbody");
  if (!tb) return;
  markSortHeaders();
  const q = resultsQuery();
  let data;
  try {
    data = await api(`/api/results?${q}`);
  } catch (err) {
    tb.innerHTML = `<tr><td colspan="10">${escAttr(err.message)}</td></tr>`;
    return;
  }
  if (!data.rows.length) {
    tb.innerHTML = `<tr><td colspan="10">Пусто — фильтры или прогон ещё без строк</td></tr>`;
  } else {
    tb.innerHTML = data.rows
      .map((r) => {
        const oosV = r.oos_return_pct;
        const retV = r.return_pct;
        const pfV = r.oos_profit_factor != null ? r.oos_profit_factor : r.profit_factor;
        const wrV = r.oos_winrate_pct != null ? r.oos_winrate_pct : r.winrate_pct;
        const ddV = r.oos_max_dd_pct != null ? r.oos_max_dd_pct : r.max_dd_pct;
        const nTr = r.oos_closed ?? r.oos_trades ?? r.closed_rounds ?? r.trades ?? null;
        const strat = r.strategy || "";
        const payload = {
          symbol: r.symbol,
          timeframe: r.timeframe,
          strategy: strat,
          score: oosV ?? 0,
        };
        return `<tr>
          <td class="col-sym" title="${r.symbol}">${r.symbol}</td>
          <td class="col-tf">${r.timeframe}</td>
          <td class="col-strat" title="${strat}">${shortStrat(strat)}</td>
          <td class="num ${clsRet(oosV)}" title="${fmt(oosV, 2)}%">${fmtPct(oosV)}</td>
          <td class="num ${clsRet(retV)}" title="${fmt(retV, 2)}%">${fmtPct(retV)}</td>
          <td class="num" title="${fmt(pfV, 4)}">${fmtPf(pfV)}</td>
          <td class="num">${fmtPct(wrV, 0)}</td>
          <td class="num">${fmtPct(ddV, 0)}</td>
          <td class="num">${fmtCount(nTr)}</td>
          <td class="col-act"><button type="button" class="linkish" data-add='${JSON.stringify(
            payload
          )}' title="Добавить в User list (My Top). Score = текущий OOS %. Повтор с тем же symbol+TF+strategy обновит строку.">+top</button></td>
        </tr>`;
      })
      .join("");
    tb.querySelectorAll("[data-add]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const payload = JSON.parse(btn.getAttribute("data-add"));
        await api("/api/top/user", {
          method: "POST",
          body: JSON.stringify({ ...payload, note: "from results" }),
        });
        alert(`Added ${payload.symbol}`);
      });
    });
  }
  const arrow = resultsState.order === "asc" ? "↑" : "↓";
  $("#results-meta").textContent =
    `shown ${data.rows.length} / total ${fmtCount(data.total)}` +
    (data.run_id != null ? ` · run #${data.run_id}` : " · all runs") +
    ` · sort ${data.sort} ${arrow} · OOS metrics · PF ∞ = no losses`;
  try {
    const job = await api("/api/run/status");
    const line = $("#results-run-line");
    if (line) {
      const run = job.run || job.last_run;
      if (run) {
        line.textContent =
          `прогон #${run.id} ${job.status || run.status} ${fmtCount(job.done)}/${fmtCount(job.total)}` +
          ` · ok ${fmtCount(job.ok)} · err ${fmtCount(job.err)} · ${job.pct ?? run.pct ?? 0}%` +
          (run.stop_loss_pct != null ? ` · SL ${run.stop_loss_pct}%` : "");
      } else {
        line.textContent = "прогона нет";
      }
    }
  } catch (_) {}
}

function ensureResultsLive() {
  stopResultsLive();
  const live = $("#f-live");
  if (!live || !live.checked) return;
  resultsLiveTimer = setInterval(() => {
    if (!$("#tab-results")?.classList.contains("active")) return;
    loadResults();
  }, 8000);
}

function stopResultsLive() {
  if (resultsLiveTimer) {
    clearInterval(resultsLiveTimer);
    resultsLiveTimer = null;
  }
}

function setupResults() {
  $("#btn-load-results")?.addEventListener("click", loadResults);
  $("#f-live")?.addEventListener("change", () => {
    if ($("#f-live").checked) {
      loadResults();
      ensureResultsLive();
    } else stopResultsLive();
  });
  ["f-run", "f-tf", "f-strat", "f-limit"].forEach((id) => {
    $(`#${id}`)?.addEventListener("change", loadResults);
  });
  ["f-symbol", "f-oos", "f-pf", "f-wr", "f-dd", "f-n"].forEach((id) => {
    $(`#${id}`)?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") loadResults();
    });
  });
  $$("#results-table thead th.sortable").forEach((th) => {
    th.addEventListener("click", () => {
      const col = th.dataset.sort;
      if (resultsState.sort === col) {
        resultsState.order = resultsState.order === "desc" ? "asc" : "desc";
      } else {
        resultsState.sort = col;
        resultsState.order = col === "symbol" || col === "timeframe" || col === "strategy" ? "asc" : "desc";
      }
      loadResults();
    });
  });
  fillResultsRuns();
}

async function loadAutoTop() {
  const data = await api("/api/top/auto?limit=38");
  const tb = $("#auto-top-table tbody");
  const emptyMsg = data.message
    || "Пока пусто — дождись prefetch done и нажми «Прогон правда SL 1.5%»";
  if (!data.tickers || !data.tickers.length) {
    tb.innerHTML = `<tr><td colspan="11">${escAttr(emptyMsg)}</td></tr>`;
    const vbtn = $("#btn-truth-verify");
    if (vbtn && !data.truth) vbtn.disabled = true;
    return;
  }
  const vbtn = $("#btn-truth-verify");
  if (vbtn) vbtn.disabled = false;
  tb.innerHTML = data.tickers
    .map((r) => {
      const nOos = oosN(r);
      const pf = r.oos_profit_factor ?? r.profit_factor;
      const wr = r.oos_winrate_pct ?? r.winrate_pct;
      const dd = r.oos_max_dd_pct ?? r.max_dd_pct;
      const stops = r.stops ?? r.stop_exits ?? 0;
      const takes = r.takes;
      const avgT = r.avg_take_pct;
      const oosTip = `OOS ${fmt(r.oos_return_pct, 2)}% со стопом 1.5%. Сделок OOS: ${fmtCount(nOos)}, кругов: ${fmtCount(r.n_rounds ?? r.closed_rounds)}.`;
      return `<tr>
      <td title="${escAttr(`Место ${r.rank} / ${data.count} · run #${data.run?.id || "—"}`)}">${r.rank}</td>
      <td title="${escAttr(r.symbol)}">${r.symbol}</td>
      <td>${r.timeframe}</td>
      <td title="${escAttr(r.strategy || "")}">${shortStrat(r.strategy)}</td>
      <td class="num ${clsRet(r.oos_return_pct)}" title="${escAttr(oosTip)}">${fmtPct(r.oos_return_pct)}</td>
      <td class="num" title="${escAttr(`Стопы: ${stops}. Средний стоп ${fmt(r.avg_stop_pct)}% (цель −1.5).`)}">${fmtCount(stops)}</td>
      <td class="num" title="${escAttr(`Тейки (выход по SELL): ${takes ?? "—"}`)}">${takes == null ? "—" : fmtCount(takes)}</td>
      <td class="num ${clsRet(avgT)}" title="${escAttr(`Средний тейк ${fmt(avgT)}%`)}">${avgT == null ? "—" : fmtPct(avgT)}</td>
      <td class="num">${fmtPf(pf)}</td>
      <td class="num">${fmtPct(wr, 0)}</td>
      <td class="num">${fmtPct(dd, 0)}</td>
    </tr>`;
    })
    .join("");
}

function applyTruthStatus(st) {
  const line = $("#truth-status");
  const runBtn = $("#btn-truth-run");
  const verBtn = $("#btn-truth-verify");
  if (!line || !runBtn) return;
  const pf = st.prefetch || {};
  const run = st.run;
  const bits = [
    `prefetch ${pf.status || "idle"} ${pf.pct ?? 0}% (${pf.done ?? 0}/${pf.total ?? 0})`,
    st.prefetch_ready ? "готово к прогону" : st.reason,
  ];
  if (run) {
    bits.push(
      `прогон #${run.id} ${run.status} ${run.done_jobs}/${run.total_jobs} ok ${run.ok_jobs}`
    );
  } else {
    bits.push("честного прогона ещё нет");
  }
  line.textContent = bits.filter(Boolean).join(" · ");
  runBtn.disabled = !(st.can_start || st.can_resume);
  if (st.can_resume) {
    runBtn.textContent = "Продолжить правду SL 1.5%";
    runBtn.title = "Resume честного прогона со стопом 1.5%.";
  } else {
    runBtn.textContent = "Прогон правда SL 1.5%";
    runBtn.title =
      "Старт только когда prefetch = done. Full · 1h/4h/1d · стоп 1.5% · сделки в БД.";
  }
  if (verBtn) verBtn.disabled = !st.can_verify;
}

async function refreshTruthStatus() {
  try {
    const st = await api("/api/truth/status");
    applyTruthStatus(st);
    return st;
  } catch (_) {
    return null;
  }
}

async function startTruthRun() {
  const candle = getCandlePayload();
  try {
    const j = await api("/api/run/truth", {
      method: "POST",
      body: JSON.stringify({
        resume: true,
        candle_limit: candle.candle_max ? 8000 : candle.candle_limit || 1600,
      }),
    });
    updateJob(j);
    startPoll();
    await refreshTruthStatus();
  } catch (err) {
    alert(err.message);
  }
}

async function verifyTruthTop() {
  const meta = $("#truth-verify-meta");
  const wrap = $("#truth-verify-wrap");
  const tb = $("#truth-verify-table tbody");
  const box = $("#truth-audit-chart-box");
  if (meta) {
    meta.hidden = false;
    meta.textContent = "сверяю стопы/тейки по локальным свечам…";
  }
  try {
    const data = await api("/api/top/verify", {
      method: "POST",
      body: JSON.stringify({ with_charts: false, limit: 38 }),
    });
    if (!data.truth) {
      if (meta) meta.textContent = data.message || "нет честного прогона";
      return;
    }
    if (meta) {
      meta.textContent =
        `сверка SL 1.5% · кругов ок ${data.rounds_ok} · расхождений ${data.rounds_fail} · пар ${data.count}`;
    }
    if (wrap) wrap.hidden = false;
    tb.innerHTML = (data.items || [])
      .map((it) => {
        const firstFail = (it.rounds || []).find((x) => !x.ok);
        const note = firstFail
          ? firstFail.note
          : it.rounds_ok
            ? "все круги сходятся со свечами"
            : "нет сохранённых сделок";
        const cls = it.rounds_fail ? "fail" : "ok";
        return `<tr>
          <td>${it.rank}</td>
          <td>${it.symbol}</td>
          <td>${it.timeframe}</td>
          <td class="num ok">${it.rounds_ok}</td>
          <td class="num ${cls}">${it.rounds_fail}</td>
          <td title="${escAttr(note)}" style="white-space:normal;max-width:28rem">${note}</td>
          <td><button type="button" class="linkish" data-audit="${it.result_id}">chart</button></td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="7">пусто</td></tr>`;
    tb.querySelectorAll("[data-audit]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          const ch = await api(`/api/top/audit-chart?result_id=${btn.dataset.audit}`);
          if (box && ch.url) {
            box.hidden = false;
            $("#truth-audit-chart").src = `${ch.url}?t=${Date.now()}`;
          }
        } catch (err) {
          alert(err.message);
        }
      });
    });
  } catch (err) {
    if (meta) meta.textContent = err.message;
  }
}

async function loadUserTop() {
  const data = await api("/api/top/user");
  const tb = $("#user-top-table tbody");
  tb.innerHTML = (data.items || [])
    .map((r) => {
      const tf = r.timeframe || "—";
      const st = r.strategy || "—";
      const note = r.note || "";
      return `<tr>
      <td title="${escAttr(r.symbol)}">${r.symbol}</td>
      <td title="${escAttr(r.timeframe ? `Сканер будет искать сигнал на ${r.timeframe}` : "ТФ не задан — сканер подставит 1h")}">${tf}</td>
      <td title="${escAttr(r.strategy ? r.strategy : "Стратегия не задана — сканер подставит rsi_mean_reversion")}">${st}</td>
      <td title="${escAttr(note || "Нет комментария")}">${note}</td>
      <td title="${escAttr(`Приоритет ${fmt(r.score)} — чем больше, тем выше в сканере. Это не OOS %.`)}">${fmt(r.score)}</td>
      <td><button type="button" class="linkish" data-del="${r.id}" title="Убрать из User list. Auto не изменится. / Remove from watchlist.">del</button></td>
    </tr>`;
    })
    .join("") || `<tr><td colspan="6" title="Список пуст. Добавьте форму выше или кнопку +top во вкладке Results.">Пусто</td></tr>`;
  tb.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await api(`/api/top/user/${btn.dataset.del}`, { method: "DELETE" });
      loadUserTop();
    });
  });
}

function setupTop() {
  $("#btn-auto-top").addEventListener("click", loadAutoTop);
  $("#btn-truth-run")?.addEventListener("click", startTruthRun);
  $("#btn-truth-verify")?.addEventListener("click", verifyTruthTop);
  $("#user-top-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    await api("/api/top/user", {
      method: "POST",
      body: JSON.stringify({
        symbol: fd.get("symbol"),
        timeframe: fd.get("timeframe") || null,
        strategy: fd.get("strategy") || null,
        note: fd.get("note") || "",
        score: Number(fd.get("score") || 0),
      }),
    });
    e.target.reset();
    loadUserTop();
  });
}

async function loadRuns() {
  const data = await api("/api/runs");
  const tb = $("#runs-table tbody");
  tb.innerHTML = (data.runs || [])
    .map((r) => {
      lastPausedRunId = r.status === "paused" ? r.id : lastPausedRunId;
      return `<tr>
        <td>${r.id}</td><td class="mono">${r.tag}</td><td>${r.status}</td>
        <td>${r.universe}</td>
        <td>${r.done_jobs}/${r.total_jobs}</td>
        <td>${r.ok_jobs}</td><td>${r.err_jobs}</td>
        <td>${r.created_at}</td>
        <td>${
          r.status === "paused"
            ? `<button type="button" class="linkish" data-resume="${r.id}">resume</button>`
            : ""
        }</td>
      </tr>`;
    })
    .join("") || `<tr><td colspan="9">Нет прогонов</td></tr>`;
  tb.querySelectorAll("[data-resume]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const j = await api("/api/run/resume", {
        method: "POST",
        body: JSON.stringify({ run_id: Number(btn.dataset.resume) }),
      });
      updateJob(j);
      startPoll();
    });
  });
}

async function loadLearn() {
  const s = await api("/api/learn/summary");
  const by = s.by_label || {};
  $("#learn-summary").innerHTML = [
    `samples <b>${s.samples}</b>`,
    `clustered <b>${s.clustered}</b>`,
    `good <b>${by.good || 0}</b>`,
    `neutral <b>${by.neutral || 0}</b>`,
    `bad <b>${by.bad || 0}</b>`,
    `thin <b>${by.thin || 0}</b>`,
    `rules <b>${s.rules}</b>`,
  ].join(" · ");

  const clusters = (s.latest_clusters && s.latest_clusters.clusters) || [];
  const rulesMap = {};
  const rules = await api("/api/learn/rules");
  (rules.rules || []).forEach((r) => {
    if (r.scope === "cluster") rulesMap[r.scope_key] = r.payload;
  });

  const ctb = $("#clusters-table tbody");
  ctb.innerHTML =
    clusters
      .map((c) => {
        const st = typeof c.stats === "string" ? JSON.parse(c.stats) : c.stats || {};
        const rule = rulesMap[String(c.cluster_id)] || {};
        return `<tr>
          <td>${c.cluster_id}</td>
          <td>${c.size}</td>
          <td>${fmt(st.good_pct, 1)}</td>
          <td class="${clsRet(st.avg_oos_return_pct)}">${fmt(st.avg_oos_return_pct)}</td>
          <td>${fmt(st.avg_oos_pf)}</td>
          <td>${fmt(st.avg_oos_dd)}</td>
          <td>${rule.action || "—"}</td>
          <td style="white-space:normal;max-width:28rem">${rule.rationale || ""}</td>
        </tr>`;
      })
      .join("") || `<tr><td colspan="8">Нет кластеров — собери samples бэктестом, затем Build</td></tr>`;

  const rtb = $("#rules-table tbody");
  rtb.innerHTML =
    (rules.rules || [])
      .map(
        (r) => `<tr>
      <td>${r.scope}</td><td>${r.scope_key || "—"}</td><td>${r.name}</td>
      <td>${r.enabled ? "yes" : "no"}</td>
      <td style="white-space:normal;max-width:36rem" class="mono">${JSON.stringify(
        r.payload
      )}</td>
    </tr>`
      )
      .join("") || `<tr><td colspan="5">—</td></tr>`;
}

function setupLearn() {
  $("#btn-cluster").addEventListener("click", async () => {
    const n = Number($("#n-clusters").value || 8);
    try {
      const out = await api("/api/learn/cluster", {
        method: "POST",
        body: JSON.stringify({ n_clusters: n }),
      });
      if (!out.ok) alert(out.error || "failed");
      loadLearn();
    } catch (err) {
      alert(err.message);
    }
  });
  $("#btn-rules").addEventListener("click", loadLearn);
}

document.addEventListener("DOMContentLoaded", async () => {
  setupTabs();
  fillStrategies();
  setupSettings();
  setupPrefetch();
  setupChart();
  setupGuide();
  setupLaunch();
  setupRunForm();
  setupTop();
  setupResults();
  setupLearn();
  setupSignals();
  await loadModes();
  // prefetch status отдельно — /api/meta тормозит на большой БД
  api("/api/prefetch/status")
    .then((j) => {
      updatePrefetchUi(j);
      updateVeroStoreUi(j.store || {});
      ensurePrefetchPoll(j);
    })
    .catch(() => {});
  refreshTruthStatus().catch(() => {});
  await Promise.all([
    loadMeta().catch(() => {}),
    api("/api/run/status")
      .then((j) => {
        updateJob(j);
        ensurePollFromJob(j);
      })
      .catch(() => {}),
  ]);
});
