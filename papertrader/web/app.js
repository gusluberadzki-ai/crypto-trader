/* PaperTrader dashboard — works against the live API or a static data.json export (GitHub Pages). */
(() => {
  "use strict";
  const LWC = window.LightweightCharts;
  const STATIC = !!window.PT_STATIC;
  if (STATIC) document.body.classList.add("static");

  const COLORS = { daily_trend: "#2ebd85", breakout: "#5fb8ff", ensemble: "#8b9dff", trend: "#36c2b4", mean_reversion: "#f5b14c", ml: "#d48cff", buy_hold: "#8a94a6" };
  const LABELS = { daily_trend: "Daily trend", breakout: "Breakout", ensemble: "Ensemble", trend: "Hourly trend", mean_reversion: "Mean reversion", ml: "Machine learning", buy_hold: "Buy & hold" };
  const DESC = {
    daily_trend: "Research winner. Holds a coin while it's above its 100-day average and up over 30 days; altcoins only while BTC is in an uptrend. Checks once a day.",
    breakout: "Buys when a coin closes above its 30-day high, sells when it drops below its 20-day low. Same BTC filter. Checks once a day.",
    ensemble: "Combines the trend, dip-buying and ML votes and only buys when they agree strongly enough.",
    trend: "Rides established uptrends (moving averages, MACD, ADX) and sells when the trend breaks.",
    mean_reversion: "Waits for sharp oversold dips in an intact uptrend, buys, and sells on the bounce.",
    ml: "A machine-learning model predicts the next 6 hours. It only trades while a model has passed validation.",
    buy_hold: "The benchmark: bought equal amounts of BTC, ETH and SOL on day one and never sells.",
  };
  const REASON = { stop_loss: "stop-loss", trailing_stop: "trailing stop", take_profit: "take-profit", signal: "strategy exit" };
  const S = { tab: "overview", pf: null, tradeFilter: "all", symbol: null, expanded: new Set(), D: {}, charts: {}, priceLines: [] };
  const $ = (id) => document.getElementById(id);

  // ---------------------------------------------------------------- formatting
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (v, d = 2, cur = "$") => v == null ? "—" : (v < 0 ? "−" : "") + cur + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sMoney = (v, d = 2, cur = "$") => v == null ? "—" : (v >= 0 ? "+" : "−") + cur + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2, sign = true) => { if (v == null) return "—"; const x = Math.abs(v * 100) < Math.pow(10, -d) / 2 ? 0 : v * 100;
    return (sign && x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(d) + "%"; };
  const cls = (v) => v == null || Math.abs(v) < 1e-9 ? "" : v > 0 ? "up" : "down";
  const price = (p) => p == null ? "—" : p >= 1000 ? p.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })
    : p >= 1 ? p.toFixed(2) : p.toFixed(5);
  const coin = (s) => (s || "").replace(/USDT$/, "");
  const ago = (ms) => { const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return "just now"; if (s < 3600) return `${(s / 60) | 0}m ago`; if (s < 86400) return `${(s / 3600) | 0}h ago`; return `${(s / 86400) | 0}d ago`; };
  const dt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const day = (ms) => new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
  const clock = (ms) => new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const dur = (ms) => { const h = ms / 3.6e6; return h < 1 ? `${Math.max(1, Math.round(h * 60))}m` : h < 48 ? `${h.toFixed(h < 10 ? 1 : 0)}h` : `${(h / 24).toFixed(1)}d`; };
  const intervalMs = (i) => ({ m: 6e4, h: 3.6e6, d: 8.64e7 }[i.slice(-1)] * parseInt(i));
  const toast = (text, kind = "") => { const el = document.createElement("div"); el.className = `toast ${kind}`; el.textContent = text;
    $("toasts").appendChild(el); setTimeout(() => el.remove(), 5000); };
  const coinBadge = (s) => `<div class="coin ${coin(s)}">${coin(s).slice(0, 3)}</div>`;

  // ---------------------------------------------------------------- data
  async function fetchJSON(path) { const r = await fetch(path, { cache: "no-store" }); if (!r.ok) throw new Error(path + " " + r.status); return r.json(); }
  async function loadAll() {
    if (STATIC) { S.D = await fetchJSON(`data.json?t=${Date.now()}`); return; }
    const names = ["status", "markets", "portfolios", "positions", "equity", "models", "events", "backtests", "latest", "sims"];
    const paths = { positions: "/api/positions", events: "/api/events?limit=120" };
    const res = await Promise.all(names.map((n) => fetchJSON(paths[n] || `/api/${n}`).catch(() => null)));
    const D = {}; names.forEach((n, i) => D[n] = res[i]);
    D.trades = await fetchJSON("/api/trades?limit=1000").catch(() => []);
    D.decisions = await fetchJSON("/api/decisions?limit=300").catch(() => []);
    D.backtest_details = S.D.backtest_details || {};
    S.D = { ...S.D, ...D };
  }
  async function candles(sym) {
    if (STATIC) { const D = S.D; return { ...D.candles[sym], markers: D.markers?.[S.pf]?.[sym] || [], positions: D.chart_positions?.[S.pf]?.[sym] || [] }; }
    return fetchJSON(`/api/candles?symbol=${sym}&limit=600&portfolio=${S.pf}`);
  }
  async function backtestDetail(id) {
    if (S.D.backtest_details?.[id]) return S.D.backtest_details[id];
    if (STATIC) return null;
    const d = await fetchJSON(`/api/backtests/${id}`); (S.D.backtest_details ||= {})[id] = d; return d;
  }
  const pf = (name) => (S.D.portfolios || []).find((p) => p.name === name);

  // ---------------------------------------------------------------- charts
  const baseOpts = () => ({
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#8d97a9", fontFamily: "Inter, system-ui, sans-serif", fontSize: 11, attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: "rgba(255,255,255,0.04)" } },
    rightPriceScale: { borderVisible: false }, timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
    crosshair: { mode: 0, vertLine: { color: "#3a4559", labelBackgroundColor: "#1f2733" }, horzLine: { color: "#3a4559", labelBackgroundColor: "#1f2733" } },
    handleScroll: { vertTouchDrag: false }, handleScale: { axisPressedMouseMove: false },
  });
  function chart(id, extra = {}) {
    if (!S.charts[id]) S.charts[id] = { c: LWC.createChart($(id), { ...baseOpts(), ...extra }), series: {} };
    return S.charts[id];
  }
  const dollars = { localization: { priceFormatter: (p) => "$" + Math.round(p).toLocaleString("en-US") } };

  // ---------------------------------------------------------------- overview
  function renderChips() {
    const names = (S.D.portfolios || []).map((p) => p.name);
    $("pfChips").innerHTML = names.map((n) => { const p = pf(n);
      return `<button class="chip ${n === S.pf ? "active" : ""}" data-pf="${n}"><i class="sw" style="background:${COLORS[n]}"></i>${LABELS[n] || n}<span class="r ${cls(p.total_return)}">${pct(p.total_return, 1)}</span></button>`; }).join("");
    $("pfChips").querySelectorAll(".chip").forEach((b) => b.onclick = () => { S.pf = b.dataset.pf; S.tradeFilter = S.pf; renderAll(); });
  }

  function strategyStatus(name) {
    const p = pf(name); if (!p) return "";
    if (p.halted) return `<span class="tag loss">paused</span><br>${esc(p.halt_reason)}`;
    const held = (S.D.positions || []).filter((x) => x.portfolio === name).map((x) => coin(x.symbol));
    if (held.length) return `<span class="tag OPEN">holding</span><br>${held.join(", ")}`;
    if (name === "ml" && !(S.D.models || []).some((m) => m.active)) return `<span class="tag warn">sitting out</span><br>no approved model yet`;
    return `<span class="tag">in cash</span><br>waiting for a signal`;
  }

  function renderHero() {
    const p = pf(S.pf); if (!p) return;
    const bh = pf("buy_hold"), st = S.D.status || {};
    $("heroLabel").textContent = `${LABELS[S.pf]} · paper money`;
    $("heroEquity").textContent = money(p.equity);
    $("heroPnl").innerHTML = `<span class="${cls(p.pnl)}">${sMoney(p.pnl)} (${pct(p.total_return)})</span>`;
    $("heroVs").innerHTML = S.pf === "buy_hold" ? `<span class="muted">benchmark</span>`
      : `<span class="${cls(p.vs_buy_hold)}">${p.vs_buy_hold >= 0 ? "ahead of" : "behind"} buy & hold by ${Math.abs((p.vs_buy_hold || 0) * 100).toFixed(2)} pts</span>`;
    $("heroStatus").innerHTML = strategyStatus(S.pf);
    const step = st.interval ? intervalMs(st.interval) : 3.6e6;
    const next = st.last_bar ? st.last_bar + 2 * step + 9 * 6e4 : null;
    const updated = st.generated_at || Date.now();
    $("heroFoot").innerHTML = `<span>Updated ${ago(updated)}</span><span>${next ? "Next decision ≈ " + clock(Math.max(next, Date.now())) : ""}</span><span>Running ${p.days ? p.days.toFixed(1) : "0"} days</span>`;
    // equity sparkline: selected strategy vs buy & hold
    const ch = chart("heroChart", { ...dollars, rightPriceScale: { visible: false }, timeScale: { visible: false, borderVisible: false }, crosshair: { mode: 1, vertLine: { visible: false }, horzLine: { visible: false } } });
    const series = S.D.equity?.series || {};
    if (!ch.series.main) {
      ch.series.bh = ch.c.addLineSeries({ color: "rgba(141,151,169,.6)", lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      ch.series.main = ch.c.addAreaSeries({ lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
    }
    const col = COLORS[S.pf];
    ch.series.main.applyOptions({ lineColor: col, topColor: col + "55", bottomColor: col + "00" });
    const live = (arr, val) => { const a = [...(arr || [])]; const t = Math.floor(Date.now() / 1000); if (a.length && t > a[a.length - 1].time) a.push({ time: t, value: val }); return a; };
    const pts = live(series[S.pf], p.equity);
    $("heroChart").style.visibility = pts.length >= 3 ? "visible" : "hidden";
    ch.series.main.setData(pts);
    ch.series.bh.setData(S.pf === "buy_hold" ? [] : live(series.buy_hold, bh?.equity));
    ch.c.timeScale().fitContent();
    $("haltBanner").hidden = !p.halted; $("haltText").textContent = p.halted ? `${LABELS[S.pf]} paused new trades: ${p.halt_reason}` : "";
  }

  function renderStats() {
    const p = pf(S.pf); if (!p) return;
    const stats = [
      ["Win rate", p.win_rate == null ? "—" : pct(p.win_rate, 0, false), `${p.trades} closed trades`],
      ["Avg trade", pct(p.avg_trade_pct), p.profit_factor != null ? `profit factor ${p.profit_factor.toFixed(2)}` : "per closed trade"],
      ["Max drawdown", pct(p.max_drawdown), "worst dip from a peak"],
      ["Best trade", p.best_trade_pct == null ? "—" : pct(p.best_trade_pct), p.worst_trade_pct == null ? "" : `worst ${pct(p.worst_trade_pct)}`],
      ["Invested", pct(p.exposure, 0, false), `${p.open_positions} open · ${money(p.cash, 0)} cash`],
      ["Fees paid", money(p.fees_paid), "0.1% per buy/sell"],
    ];
    $("statGrid").innerHTML = stats.map(([l, v, s]) => `<div class="stat"><div class="l">${l}</div><div class="v ${l === "Avg trade" || l === "Best trade" ? cls(parseFloat(v.replace("−", "-"))) : ""}">${v}</div><div class="s">${esc(s)}</div></div>`).join("");
  }

  function renderPositions() {
    const rows = (S.D.positions || []).filter((x) => x.portfolio === S.pf);
    $("posHint").textContent = rows.length ? `${rows.length} open` : "";
    if (!rows.length) { $("positions").innerHTML = `<div class="empty">No open positions — ${LABELS[S.pf]} is fully in cash right now.</div>`; return; }
    $("positions").innerHTML = rows.map((p) => {
      let bar = "";
      if (p.stop != null && p.take_profit) {
        const lo = p.stop, hi = p.take_profit, pos = (v) => Math.min(100, Math.max(0, (v - lo) / (hi - lo) * 100));
        bar = `<div class="range"><i class="entry" style="left:${pos(p.entry_price)}%"></i><i style="left:${pos(p.price)}%"></i></div>
          <div class="range-lbl"><span>stop ${price(p.stop)}${p.trailing ? " (trailing)" : ""}</span><span>target ${price(p.take_profit)}</span></div>`;
      }
      const id = "pos-" + p.trade_id;
      return `<div class="pos-card"><div class="row click" style="padding:0;background:none" data-x="${id}">${coinBadge(p.symbol)}<div class="main">
        <div class="t1"><span>${coin(p.symbol)} · ${money(p.value, 0)}</span><span class="${cls(p.pnl)} mono">${sMoney(p.pnl)}</span></div>
        <div class="t2"><span>bought at ${price(p.entry_price)} · now ${price(p.price)}</span><span class="${cls(p.pnl_pct)} mono">${pct(p.pnl_pct)}</span></div></div></div>
        ${bar}${S.expanded.has(id) ? `<div class="expl" style="margin:10px 0 0;border-radius:10px"><b>Why it bought</b>${esc(p.explanation)}</div>` : ""}</div>`;
    }).join("");
    bindExpand($("positions"), renderPositions);
  }

  function renderThinking() {
    const rows = (S.D.latest || []).filter((d) => d.portfolio === S.pf);
    $("thinkHint").textContent = rows.length ? `at the ${clock(rows[0].bar_time + intervalMs(S.D.status?.interval || "1h"))} candle` : "";
    if (!rows.length) { $("thinking").innerHTML = `<div class="empty">No decisions yet.</div>`; return; }
    $("thinking").innerHTML = rows.map((d) => {
      const text = d.explanation.replace(/^(HOLD|BUY|SELL|BLOCKED)\s+[^.]*?at \$[\d,.]+\.?\s*/, "").replace(/^\(confidence \d+%\)\.\s*Why:\s*/, "Buying because ");
      return `<div class="think">${coinBadge(d.symbol)}<div class="main" style="min-width:0"><div class="t1">${coin(d.symbol)} <span class="tag ${d.action}">${d.action}</span><span class="when">${ago(d.time)}</span></div>
        <p>${esc(text)}</p></div></div>`;
    }).join("");
  }

  function activityItems(name) {
    const items = [];
    for (const t of S.D.trades || []) if (!name || t.portfolio === name) items.push({ time: t.exit_time, kind: "closed", t });
    for (const d of S.D.decisions || []) if ((!name || d.portfolio === name) && (d.action === "BUY" || d.action === "BLOCKED")) items.push({ time: d.time, kind: d.action, d });
    return items.sort((a, b) => b.time - a.time);
  }
  function renderActivity() {
    const items = activityItems(S.pf).slice(0, 6);
    if (!items.length) { $("activity").innerHTML = `<div class="empty">Nothing yet. Activity appears when the bot buys, sells or gets blocked by a risk rule.</div>`; return; }
    $("activity").innerHTML = items.map((it) => {
      if (it.kind === "closed") { const t = it.t;
        return `<div class="row">${coinBadge(t.symbol)}<div class="main"><div class="t1"><span>Sold ${coin(t.symbol)}</span><span class="${cls(t.pnl)} mono">${sMoney(t.pnl)}</span></div>
          <div class="t2"><span>${REASON[t.exit_reason] || t.exit_reason} · ${ago(t.exit_time)}</span><span class="${cls(t.pnl_pct)} mono">${pct(t.pnl_pct)}</span></div></div></div>`; }
      const d = it.d;
      return `<div class="row">${coinBadge(d.symbol)}<div class="main"><div class="t1"><span>${d.action === "BUY" ? "Bought" : "Wanted to buy"} ${coin(d.symbol)}</span><span class="mono">${price(d.price)}</span></div>
        <div class="t2"><span>${d.action === "BUY" ? `confidence ${(d.confidence * 100).toFixed(0)}%` : "blocked by a risk rule"} · ${ago(d.time)}</span></div></div></div>`;
    }).join("");
  }

  // ---------------------------------------------------------------- trades
  function renderTradeTab() {
    const names = ["all", ...(S.D.portfolios || []).map((p) => p.name).filter((n) => n !== "buy_hold")];
    $("tradeChips").innerHTML = names.map((n) => `<button class="chip ${n === S.tradeFilter ? "active" : ""}" data-f="${n}">${n === "all" ? "All strategies" : `<i class="sw" style="background:${COLORS[n]}"></i>${LABELS[n]}`}</button>`).join("");
    $("tradeChips").querySelectorAll(".chip").forEach((b) => b.onclick = () => { S.tradeFilter = b.dataset.f; renderTradeTab(); });
    const f = S.tradeFilter === "all" ? null : S.tradeFilter;
    const closed = (S.D.trades || []).filter((t) => t.portfolio !== "buy_hold" && (!f || t.portfolio === f));
    const open = (S.D.positions || []).filter((p) => p.portfolio !== "buy_hold" && (!f || p.portfolio === f));
    const wins = closed.filter((t) => t.pnl > 0), losses = closed.filter((t) => t.pnl <= 0);
    const total = closed.reduce((a, t) => a + t.pnl, 0), fees = closed.reduce((a, t) => a + t.entry_fee + t.exit_fee, 0);
    const avg = (arr) => arr.length ? arr.reduce((a, t) => a + t.pnl_pct, 0) / arr.length : null;
    const best = closed.reduce((b, t) => !b || t.pnl_pct > b.pnl_pct ? t : b, null), worst = closed.reduce((b, t) => !b || t.pnl_pct < b.pnl_pct ? t : b, null);
    $("tradeStats").innerHTML = [
      ["Realised P/L", `<span class="${cls(total)}">${sMoney(total)}</span>`, `${closed.length} closed · ${open.length} open`],
      ["Wins / losses", `${wins.length} / ${losses.length}`, closed.length ? `${(wins.length / closed.length * 100).toFixed(0)}% win rate` : "no trades yet"],
      ["Fees paid", money(fees), "on closed trades"],
      ["Avg win", `<span class="up">${avg(wins) == null ? "—" : pct(avg(wins))}</span>`, "per winning trade"],
      ["Avg loss", `<span class="down">${avg(losses) == null ? "—" : pct(avg(losses))}</span>`, "per losing trade"],
      ["Best / worst", best ? `<span class="${cls(best.pnl_pct)}">${pct(best.pnl_pct, 1)}</span>` : "—", worst ? `worst ${pct(worst.pnl_pct, 1)} (${coin(worst.symbol)})` : ""],
    ].map(([l, v, s]) => `<div class="stat"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${esc(s)}</div></div>`).join("");

    const items = [
      ...open.map((p) => ({ open: true, time: Infinity, p })),
      ...closed.map((t) => ({ open: false, time: t.exit_time, t })),
    ];
    if (!items.length) { $("tradeList").innerHTML = `<div class="empty">No trades yet for this selection. Trades show up here the moment the bot buys or sells.</div>`; return; }
    $("tradeList").innerHTML = items.map((it) => {
      if (it.open) { const p = it.p, id = "op-" + p.trade_id;
        return `<div class="row click open-trade" data-x="${id}">${coinBadge(p.symbol)}<div class="main"><div class="t1"><span>${coin(p.symbol)} <span class="tag OPEN">OPEN</span></span><span class="${cls(p.pnl)} mono">${sMoney(p.pnl)}</span></div>
          <div class="t2"><span>${LABELS[p.portfolio]} · bought ${price(p.entry_price)} → ${price(p.price)}</span><span class="${cls(p.pnl_pct)} mono">${pct(p.pnl_pct)}</span></div></div></div>
          ${S.expanded.has(id) ? `<div class="expl"><b>Why it bought</b>${esc(p.explanation)}</div>` : ""}`; }
      const t = it.t, id = "tr-" + t.id;
      return `<div class="row click" data-x="${id}">${coinBadge(t.symbol)}<div class="main"><div class="t1"><span>${coin(t.symbol)} <span class="tag ${t.pnl > 0 ? "win" : "loss"}">${REASON[t.exit_reason] || t.exit_reason}</span></span><span class="${cls(t.pnl)} mono">${sMoney(t.pnl)}</span></div>
        <div class="t2"><span>${LABELS[t.portfolio]} · ${day(t.exit_time)} · held ${dur(t.exit_time - t.entry_time)}</span><span class="${cls(t.pnl_pct)} mono">${pct(t.pnl_pct)}</span></div>
        <div class="t2"><span class="mono">${price(t.entry_price)} → ${price(t.exit_price)}</span><span class="faint">fees ${money(t.entry_fee + t.exit_fee)}</span></div></div></div>
        ${S.expanded.has(id) ? `<div class="expl"><b>Why it bought</b>${esc(t.entry_explanation)}<b>Why it sold</b>${esc(t.exit_explanation)}</div>` : ""}`;
    }).join("");
    bindExpand($("tradeList"), renderTradeTab);
  }
  function bindExpand(root, rerender) {
    root.querySelectorAll("[data-x]").forEach((el) => el.onclick = () => { const id = el.dataset.x; S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id); rerender(); });
  }

  // ---------------------------------------------------------------- strategies
  function renderLeaderboard() {
    const rows = [...(S.D.portfolios || [])].sort((a, b) => b.total_return - a.total_return);
    $("leaderboard").innerHTML = rows.map((r, i) => `<div class="lb ${r.name === S.pf ? "selected" : ""}" data-pf="${r.name}">
      <div class="top"><div class="rank ${i === 0 ? "first" : ""}">${i + 1}</div><i class="sw" style="width:9px;height:9px;border-radius:50%;background:${COLORS[r.name]}"></i>
      <div class="name">${LABELS[r.name]}${r.halted ? ' <span class="tag loss">paused</span>' : ""}</div><div class="ret ${cls(r.total_return)}">${pct(r.total_return)}</div></div>
      <div class="desc">${DESC[r.name] || ""}</div>
      <div class="meta"><span>value <b>${money(r.equity, 0)}</b></span>${r.name === "buy_hold" ? "" : `<span>vs B&H <b class="${cls(r.vs_buy_hold)}">${pct(r.vs_buy_hold)}</b></span><span>trades <b>${r.trades}</b></span><span>win <b>${r.win_rate == null ? "—" : pct(r.win_rate, 0, false)}</b></span>`}<span>max DD <b>${pct(r.max_drawdown)}</b></span></div></div>`).join("");
    $("leaderboard").querySelectorAll(".lb").forEach((el) => el.onclick = () => { S.pf = el.dataset.pf; S.tradeFilter = S.pf; switchTab("overview"); renderAll(); });
    const p = pf(S.pf);
    $("sigNote").textContent = p?.significance_note ? `Honesty check: ${p.significance_note}.` : "";
  }
  function renderEquity() {
    const series = S.D.equity?.series || {}, names = Object.keys(series);
    const ch = chart("equityChart", dollars);
    for (const n of names) {
      if (!ch.series[n]) ch.series[n] = ch.c.addLineSeries({ color: COLORS[n] || "#aaa", lineWidth: 2, lineStyle: n === "buy_hold" ? 2 : 0, priceLineVisible: false, lastValueVisible: false });
      ch.series[n].setData(series[n]); ch.series[n].applyOptions({ lineWidth: n === S.pf ? 3 : 1.5 });
    }
    ch.c.timeScale().fitContent();
    $("eqLegend").innerHTML = names.map((n) => `<span><i class="${n === "buy_hold" ? "dashed" : ""}" style="background:${COLORS[n]}"></i>${LABELS[n] || n}</span>`).join("");
  }
  function renderModels() {
    const ms = S.D.models || [], act = ms.find((m) => m.active), last = ms[0];
    let txt;
    if (act) txt = `Model v${act.version} is active. On recent data it never trained on, its buy signals averaged ${pct(act.val_expectancy)} per trade after fees (AUC ${act.val_auc.toFixed(3)}; 0.5 = coin flip).`;
    else if (last) txt = `No model is trading yet. The latest attempt (v${last.version}) was rejected: ${last.reason}. A new one is trained every day and only goes live if it beats random entries after fees — so the ML strategy safely sits in cash until then.`;
    else txt = "No model trained yet.";
    $("modelExplain").textContent = txt;
    $("retrainBtn").disabled = !!S.D.status?.training;
    $("modelList").innerHTML = ms.slice(0, 10).map((m) => `<div class="row"><div class="main"><div class="t1"><span>v${m.version} ${m.active ? '<span class="tag win">active</span>' : m.promoted ? "" : '<span class="tag">rejected</span>'}</span><span class="mono ${cls(m.val_expectancy)}">${pct(m.val_expectancy)}</span></div>
      <div class="t2"><span>${dt(m.created_at)} · AUC ${m.val_auc.toFixed(3)}</span><span>${m.val_trades} test signals</span></div></div></div>`).join("") || "";
  }
  function renderLog() {
    const ev = S.D.events || [];
    $("eventLog").innerHTML = ev.map((e) => `<div class="ev ${e.level}"><span class="t">${clock(e.time)}</span><span class="m">${esc(e.message)}</span></div>`).join("");
    const errs = ev.filter((e) => e.level === "error" && Date.now() - e.time < 86400000).length;
    $("sysHint").textContent = errs ? `${errs} error(s) in 24h` : "all good";
  }

  // ---------------------------------------------------------------- market
  function renderCoins() {
    const ms = S.D.markets || [];
    if (!S.symbol && ms.length) S.symbol = ms[0].symbol;
    $("coins").innerHTML = ms.map((m) => `<div class="coin-card ${m.symbol === S.symbol ? "active" : ""}" data-s="${m.symbol}">
      <div class="sym">${coin(m.symbol)}</div><div class="px" id="px-${m.symbol}">$${price(m.price)}</div><div class="ch ${cls(m.change_24h)}">${pct(m.change_24h)} 24h</div></div>`).join("");
    $("coins").querySelectorAll(".coin-card").forEach((el) => el.onclick = () => { S.symbol = el.dataset.s; renderCoins(); renderPriceChart(true); });
  }
  async function renderPriceChart(fit = false) {
    if (!S.symbol || S.tab !== "market") return;
    const d = await candles(S.symbol); if (!d || !d.candles) return;
    const ch = chart("priceChart", { localization: { priceFormatter: (p) => price(p) } });
    if (!ch.series.c) {
      ch.series.c = ch.c.addCandlestickSeries({ upColor: "#2ebd85", downColor: "#f0616d", borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f0616d" });
      ch.series.v = ch.c.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
      ch.series.v.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
      ch.series.e20 = ch.c.addLineSeries({ color: "#f5b14c", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      ch.series.e50 = ch.c.addLineSeries({ color: "#8b9dff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      fit = true;
    }
    ch.series.c.setData(d.candles);
    ch.series.v.setData(d.candles.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "rgba(46,189,133,.25)" : "rgba(240,97,109,.25)" })));
    ch.series.e20.setData(d.ema20); ch.series.e50.setData(d.ema50);
    ch.series.c.setMarkers((d.markers || []).map((m) => ({ time: m.time, position: m.action === "BUY" ? "belowBar" : "aboveBar",
      color: m.action === "BUY" ? "#2ebd85" : "#f0616d", shape: m.action === "BUY" ? "arrowUp" : "arrowDown", text: m.action === "BUY" ? "B" : "S" })));
    S.priceLines.forEach((l) => ch.series.c.removePriceLine(l)); S.priceLines = [];
    for (const p of d.positions || []) {
      S.priceLines.push(ch.series.c.createPriceLine({ price: p.entry, color: "#8d97a9", lineWidth: 1, lineStyle: 2, title: "entry" }));
      S.priceLines.push(ch.series.c.createPriceLine({ price: p.stop, color: "#f0616d", lineWidth: 1, lineStyle: 2, title: "stop" }));
      if (p.take_profit) S.priceLines.push(ch.series.c.createPriceLine({ price: p.take_profit, color: "#2ebd85", lineWidth: 1, lineStyle: 2, title: "target" }));
    }
    $("chartTitle").textContent = `${coin(S.symbol)} · ${S.D.status?.interval || "1h"} candles · ${LABELS[S.pf]} trades`;
    if (fit) ch.c.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.candles.length - 120), to: d.candles.length + 3 });
  }

  // ---------------------------------------------------------------- simulations
  function renderSims() {
    const runs = S.D.sims?.runs || [];
    const key = `${S.D.sims?.generated_at}|${(S.D.backtests || []).length}`;
    if (S.simsKey === key) return;
    S.simsKey = key;
    if (!runs.length) { $("simRuns").innerHTML = `<div class="card"><div class="empty">No simulations saved yet.</div></div>`; }
    else $("simRuns").innerHTML = runs.map((run, i) => {
      const cur = run.currency === "CHF" ? "CHF " : "$", fx = run.fx || 1;
      const rs = Object.entries(run.results).sort((a, b) => b[1].total_return - a[1].total_return);
      const best = rs[0], bh = run.results.buy_hold;
      const bestBot = rs.find(([n]) => n !== "buy_hold");
      const end = (r) => r.equity / fx;
      const takeaway = bestBot && bestBot[1].total_return > bh.total_return
        ? `The best bot (${LABELS[bestBot[0]]}) beat simply holding by ${((bestBot[1].total_return - bh.total_return) * 100).toFixed(1)} points over ${Math.round(run.days)} days.`
        : `None of the bots beat simply buying and holding over these ${Math.round(run.days)} days — after fees, holding ${bh.total_return >= 0 ? "made" : "lost"} ${money(Math.abs(end(bh) - run.start), 0, cur)} while the best bot (${LABELS[bestBot[0]]}) ${bestBot[1].total_return >= 0 ? "made" : "lost"} ${money(Math.abs(end(bestBot[1]) - run.start), 0, cur)}.`;
      return `<div class="card"><div class="sim-head"><div><div class="label">${esc(run.name)} · ${day(run.start_time)} → ${day(run.end_time)}</div>
        <div class="sim-big">${money(run.start, 0, cur)} → <span class="${cls(bestBot[1].total_return)}">${money(end(bestBot[1]), cur === "$" ? 0 : 2, cur)}</span></div>
        <div class="faint" style="font-size:12px">best bot: ${LABELS[bestBot[0]]}</div></div></div>
        <table class="sim-table"><thead><tr><th>Strategy</th><th class="n">Final</th><th class="n">Return</th><th class="n">Trades</th></tr></thead><tbody>
        ${rs.map(([n, r]) => `<tr class="${n === best[0] ? "best" : ""}"><td><i class="sw" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${COLORS[n]};margin-right:7px"></i>${LABELS[n]}</td>
          <td class="n">${money(end(r), cur === "$" ? 0 : 2, cur)}</td><td class="n ${cls(r.total_return)}">${pct(r.total_return, 1)}</td><td class="n">${n === "buy_hold" ? "—" : r.trades}</td></tr>`).join("")}
        </tbody></table>
        <div id="simChart${i}" class="chart small" style="margin-top:12px"></div>
        <div class="takeaway">${esc(takeaway)} Fees paid by the busiest bot: ${money(Math.max(...rs.map(([, r]) => r.fees_paid || 0)) / fx, 2, cur)}.</div></div>`;
    }).join("");
    runs.forEach((run, i) => {
      const fx = run.fx || 1;
      const c = LWC.createChart($(`simChart${i}`), { ...baseOpts(), localization: { priceFormatter: (p) => (run.currency === "CHF" ? "CHF " : "$") + Math.round(p).toLocaleString("en-US") } });
      for (const n of Object.keys(run.results)) {
        const s = c.addLineSeries({ color: COLORS[n], lineWidth: n === "buy_hold" ? 1 : 1.5, lineStyle: n === "buy_hold" ? 2 : 0, priceLineVisible: false, lastValueVisible: false });
        s.setData(run.equity.time.map((t, k) => ({ time: Math.floor(t / 1000), value: run.equity[n][k] / fx })));
      }
      c.timeScale().fitContent();
    });
    const bts = S.D.backtests || [];
    $("btList").innerHTML = bts.length ? bts.slice(0, 8).map((b) => { const r = b.results, bh = r.buy_hold;
      const bestBot = Object.entries(r).filter(([n]) => n !== "buy_hold").sort((a, x) => x[1].total_return - a[1].total_return)[0];
      return `<div class="row"><div class="main"><div class="t1"><span>${day(b.start_time)} → ${day(b.end_time)}</span><span class="mono ${cls(bestBot?.[1].total_return)}">${bestBot ? pct(bestBot[1].total_return, 1) : "—"}</span></div>
        <div class="t2"><span>best: ${bestBot ? LABELS[bestBot[0]] : "—"} · buy & hold ${pct(bh?.total_return, 1)}</span><span>${b.params.days}d</span></div></div></div>`; }).join("")
      : `<div class="empty">The first weekly backtest appears after the bot's first full week.</div>`;
  }

  // ---------------------------------------------------------------- status + tabs
  function renderStatus() {
    const st = S.D.status || {};
    const map = { live: ["live", "Live"], hourly: ["live", "Updated hourly"], polling: ["warn", "Polling"], reconnecting: ["warn", "Reconnecting"],
                  connecting: ["warn", "Connecting"], starting: ["warn", "Starting"], offline: ["bad", "Offline"] };
    let [k, label] = st.start_error ? ["bad", "Engine error"] : (map[st.feed] || ["warn", st.feed || "…"]);
    const age = st.generated_at ? Date.now() - st.generated_at : 0;
    if (STATIC && age > 2.5 * 3.6e6) { k = "warn"; label = "Last update " + ago(st.generated_at); }
    else if (STATIC && st.generated_at) label = "Updated " + ago(st.generated_at);
    $("feedPill").className = `pill ${k}`; $("feedText").textContent = label;
  }
  function switchTab(name) {
    S.tab = name;
    document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === "tab-" + name));
    window.scrollTo({ top: 0 });
    if (name === "market") renderPriceChart(true);
    try { history.replaceState(null, "", "#" + name); } catch {}
  }
  function renderAll() {
    if (!S.D.portfolios?.length) return;
    if (!S.pf || !pf(S.pf)) S.pf = S.D.status?.primary || S.D.portfolios[0].name;
    renderStatus(); renderChips(); renderHero(); renderStats(); renderPositions(); renderThinking(); renderActivity();
    renderTradeTab(); renderLeaderboard(); renderEquity(); renderModels(); renderLog(); renderCoins(); renderSims();
    if (S.tab === "market") renderPriceChart();
  }

  // ---------------------------------------------------------------- live updates (server mode)
  let reloadTimer = null;
  const scheduleReload = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(async () => { await loadAll().catch(() => {}); renderAll(); }, 1500); };
  function connect() {
    let delay = 1000;
    const open = () => {
      const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
      ws.onopen = () => { delay = 1000; };
      ws.onmessage = (ev) => {
        const m = JSON.parse(ev.data);
        if (m.type === "tick") { const el = $(`px-${m.symbol}`); if (el) el.textContent = "$" + price(m.price);
          const c = S.charts.priceChart; if (c?.series.c && m.symbol === S.symbol) { const k = m.bar; c.series.c.update({ time: Math.floor(k.open_time / 1000), open: k.open, high: k.high, low: k.low, close: k.close }); } }
        else if (m.type === "decision") { if (m.decision.action !== "HOLD") toast(`${m.decision.action} ${coin(m.decision.symbol)} · ${LABELS[m.decision.portfolio]}`, m.decision.action); scheduleReload(); }
        else if (["trade", "bar", "equity", "model", "backtest", "event"].includes(m.type)) scheduleReload();
        else if (m.type === "status") { S.D.status = { ...S.D.status, ...m.status }; renderStatus(); }
      };
      ws.onclose = () => { setTimeout(open, delay); delay = Math.min(delay * 2, 15000); };
    };
    open();
  }

  // ---------------------------------------------------------------- boot
  async function boot() {
    document.querySelectorAll(".tabbar button").forEach((b) => b.onclick = () => switchTab(b.dataset.tab));
    document.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => switchTab(b.dataset.goto));
    $("resumeBtn").onclick = async () => { await fetch(`/api/portfolios/${S.pf}/resume`, { method: "POST" }); scheduleReload(); };
    $("retrainBtn").onclick = async () => { $("retrainBtn").disabled = true; await fetch("/api/models/retrain", { method: "POST" }); toast("Retraining started"); };
    for (;;) { try { await loadAll(); if (S.D.portfolios?.length) break; } catch (e) { /* server still starting */ } await new Promise((r) => setTimeout(r, 3000)); }
    renderAll();
    const h = location.hash.slice(1); if (["overview", "trades", "strategies", "market", "sims"].includes(h)) switchTab(h);
    if (STATIC) setInterval(async () => { await loadAll().catch(() => {}); renderAll(); }, 5 * 60 * 1000);
    else { connect(); setInterval(scheduleReload, 60000); }
    setInterval(() => { renderStatus(); renderHero(); }, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) loadAll().then(renderAll).catch(() => {}); });
  }
  boot();
})();
