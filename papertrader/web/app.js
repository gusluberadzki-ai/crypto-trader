/* PaperTrader dashboard */
(() => {
  "use strict";
  const LWC = window.LightweightCharts;
  const COLORS = { ensemble: "#8b9dff", trend: "#36c2b4", mean_reversion: "#f5b14c", ml: "#d48cff", buy_hold: "#8a94a6" };
  const LABELS = { ensemble: "Ensemble (ML + rules)", trend: "Trend following", mean_reversion: "Mean reversion",
                   ml: "Machine learning", buy_hold: "Buy & hold (benchmark)" };
  const S = { pf: null, symbol: null, status: null, portfolios: [], markets: {}, priceLines: [], lastBarTime: {},
              feedAll: false, expanded: new Set(), backtests: [], btId: null };
  const $ = (id) => document.getElementById(id);

  // ------------------------------------------------------------- formatting
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const usd = (v, d = 2) => v == null ? "—" : (v < 0 ? "-" : "") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
  const sUsd = (v) => v == null ? "—" : (v >= 0 ? "+" : "-") + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const pct = (v, d = 2, sign = true) => v == null ? "—" : (sign && v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
  const cls = (v) => v == null || Math.abs(v) < 1e-12 ? "" : v > 0 ? "up" : "down";
  const price = (p) => p == null ? "—" : p >= 1000 ? p.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })
    : p >= 1 ? p.toFixed(3) : p.toFixed(6);
  const coin = (s) => s.replace(/USDT$/, "");
  const ago = (ms) => {
    const s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 60) return `${s | 0}s ago`; if (s < 3600) return `${(s / 60) | 0}m ago`;
    if (s < 86400) return `${(s / 3600) | 0}h ago`; return `${(s / 86400) | 0}d ago`;
  };
  const dt = (ms) => new Date(ms).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  const dur = (h) => h == null ? "—" : h < 1 ? `${Math.round(h * 60)}m` : h < 48 ? `${h.toFixed(1)}h` : `${(h / 24).toFixed(1)}d`;
  const api = async (path, opts) => {
    const r = await fetch(path, opts);
    if (!r.ok) throw new Error(`${path}: ${r.status}`);
    return r.json();
  };
  const toast = (text, kind = "") => {
    const el = document.createElement("div");
    el.className = `toast ${kind}`; el.textContent = text;
    $("toasts").appendChild(el); setTimeout(() => el.remove(), 6000);
  };
  const throttle = (fn, ms) => { let t = 0, pending = null;
    return (...a) => { const now = Date.now(); if (now - t > ms) { t = now; fn(...a); }
      else if (!pending) pending = setTimeout(() => { pending = null; t = Date.now(); fn(...a); }, ms - (now - t)); }; };

  // ------------------------------------------------------------- charts
  const chartOpts = (h) => ({
    autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: "#8a94a6", fontFamily: "Inter, system-ui, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: "rgba(255,255,255,0.035)" }, horzLines: { color: "rgba(255,255,255,0.035)" } },
    rightPriceScale: { borderColor: "#212937" },
    timeScale: { borderColor: "#212937", timeVisible: true, secondsVisible: false, rightOffset: 4 },
    crosshair: { mode: 0, vertLine: { color: "#3a4559", labelBackgroundColor: "#1d2430" }, horzLine: { color: "#3a4559", labelBackgroundColor: "#1d2430" } },
    localization: { priceFormatter: (p) => price(p) },
  });
  const pc = LWC.createChart($("priceChart"), chartOpts());
  const candles = pc.addCandlestickSeries({ upColor: "#2ebd85", downColor: "#f0616d", borderVisible: false, wickUpColor: "#2ebd85", wickDownColor: "#f0616d" });
  const volume = pc.addHistogramSeries({ priceScaleId: "", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
  const e20 = pc.addLineSeries({ color: "#f5b14c", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
  const e50 = pc.addLineSeries({ color: "#8b9dff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });

  const ec = LWC.createChart($("equityChart"), { ...chartOpts(), localization: { priceFormatter: (p) => "$" + Math.round(p).toLocaleString() } });
  const eqSeries = {};
  const bc = LWC.createChart($("btChart"), { ...chartOpts(), localization: { priceFormatter: (p) => "$" + Math.round(p).toLocaleString() } });
  let btSeries = [];

  const volBar = (b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "rgba(46,189,133,0.28)" : "rgba(240,97,109,0.28)" });

  async function loadChart(fit = false) {
    if (!S.symbol) return;
    const d = await api(`/api/candles?symbol=${S.symbol}&limit=600&portfolio=${S.pf}`);
    candles.setData(d.candles); volume.setData(d.candles.map(volBar));
    e20.setData(d.ema20); e50.setData(d.ema50);
    if (d.candles.length) S.lastBarTime[S.symbol] = d.candles[d.candles.length - 1].time;
    if (d.live && d.live.open_time / 1000 > (S.lastBarTime[S.symbol] || 0)) applyBar(d.live);
    candles.setMarkers(d.markers.map((m) => ({
      time: m.time, position: m.action === "BUY" ? "belowBar" : "aboveBar", color: m.action === "BUY" ? "#2ebd85" : "#f0616d",
      shape: m.action === "BUY" ? "arrowUp" : "arrowDown", text: m.action === "BUY" ? "B" : "S" })));
    S.priceLines.forEach((l) => candles.removePriceLine(l)); S.priceLines = [];
    for (const p of d.positions) {
      S.priceLines.push(candles.createPriceLine({ price: p.entry, color: "#8a94a6", lineWidth: 1, lineStyle: 2, title: "entry" }));
      S.priceLines.push(candles.createPriceLine({ price: p.stop, color: "#f0616d", lineWidth: 1, lineStyle: 2, title: "stop" }));
      if (p.take_profit) S.priceLines.push(candles.createPriceLine({ price: p.take_profit, color: "#2ebd85", lineWidth: 1, lineStyle: 2, title: "target" }));
    }
    if (fit) pc.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.candles.length - 160), to: d.candles.length + 4 });
  }
  function applyBar(k) {
    const b = { time: Math.floor(k.open_time / 1000), open: k.open, high: k.high, low: k.low, close: k.close, volume: k.quote_volume || k.volume };
    if (b.time < (S.lastBarTime[S.symbol] || 0)) return;
    S.lastBarTime[S.symbol] = b.time;
    candles.update(b); volume.update(volBar(b));
  }

  // ------------------------------------------------------------- symbol tabs
  function renderTabs() {
    const el = $("symbolTabs");
    el.innerHTML = S.status.symbols.map((s) => {
      const m = S.markets[s] || {};
      return `<button class="tab ${s === S.symbol ? "active" : ""}" data-s="${s}">
        <span class="sym">${coin(s)}<span class="muted"> / USDT</span></span>
        <span class="px" id="px-${s}">${price(m.price)}</span>
        <span class="ch ${cls(m.change_24h)}" id="ch-${s}">${pct(m.change_24h)} 24h</span></button>`;
    }).join("");
    el.querySelectorAll(".tab").forEach((b) => b.onclick = () => { S.symbol = b.dataset.s; renderTabs(); loadChart(true); });
  }
  function updateTab(s, p) {
    const m = S.markets[s] = S.markets[s] || {};
    const prev = m.price; m.price = p;
    const px = $(`px-${s}`); if (!px) return;
    px.textContent = price(p);
    if (prev != null && p !== prev) { px.className = "px " + (p > prev ? "up" : "down"); }
  }

  // ------------------------------------------------------------- KPIs + comparison
  async function loadPortfolios() {
    S.portfolios = await api("/api/portfolios");
    const p = S.portfolios.find((x) => x.name === S.pf); if (!p) return;
    const bh = S.portfolios.find((x) => x.name === "buy_hold");
    $("kEquity").textContent = usd(p.equity);
    $("kEquitySub").textContent = `cash ${usd(p.cash, 0)} · start ${usd(p.start_balance, 0)}`;
    $("kPnl").innerHTML = `<span class="${cls(p.pnl)}">${sUsd(p.pnl)}</span>`;
    $("kPnlSub").innerHTML = `<span class="${cls(p.total_return)}">${pct(p.total_return)}</span> · realised ${sUsd(p.realized_pnl)}`;
    if (p.name === "buy_hold") { $("kVsBh").textContent = "benchmark"; $("kVsBhSub").textContent = "equal-weight, never sells"; }
    else {
      $("kVsBh").innerHTML = `<span class="${cls(p.vs_buy_hold)}">${p.vs_buy_hold == null ? "—" : (p.vs_buy_hold >= 0 ? "+" : "") + (p.vs_buy_hold * 100).toFixed(2) + " pts"}</span>`;
      $("kVsBhSub").textContent = bh ? `buy & hold ${pct(bh.total_return)}` : "";
    }
    $("kDd").innerHTML = `<span class="${p.max_drawdown < -0.0001 ? "down" : ""}">${pct(p.max_drawdown)}</span>`;
    $("kDdSub").textContent = p.sharpe != null ? `Sharpe ${p.sharpe.toFixed(2)} · ${p.days.toFixed(1)} days` : `${(p.days || 0).toFixed(1)} days of data`;
    $("kWin").textContent = p.win_rate == null ? "—" : pct(p.win_rate, 0, false);
    $("kWinSub").textContent = `${p.trades} trades` + (p.profit_factor != null ? ` · PF ${p.profit_factor.toFixed(2)}` : "");
    $("kExp").textContent = pct(p.exposure, 0, false);
    $("kExpSub").textContent = `${p.open_positions} open · fees ${usd(p.fees_paid)}`;
    $("haltBanner").hidden = !p.halted;
    $("haltText").textContent = p.halted ? `${p.label}: new entries halted — ${p.halt_reason}` : "";

    const rows = S.portfolios.map((r) => `<tr class="click ${r.name === S.pf ? "selected" : ""}" data-pf="${r.name}">
      <td><span class="swatch" style="background:${COLORS[r.name] || "#888"}"></span>${esc(r.label)} ${r.halted ? '<span class="tag halt">halted</span>' : ""}</td>
      <td class="n">${usd(r.equity)}</td><td class="n ${cls(r.total_return)}">${pct(r.total_return)}</td>
      <td class="n ${r.name === "buy_hold" ? "" : cls(r.vs_buy_hold)}">${r.name === "buy_hold" ? "—" : pct(r.vs_buy_hold)}</td>
      <td class="n">${pct(r.max_drawdown)}</td><td class="n">${r.sharpe == null ? "—" : r.sharpe.toFixed(2)}</td>
      <td class="n">${r.trades}</td><td class="n">${r.win_rate == null ? "—" : pct(r.win_rate, 0, false)}</td>
      <td class="n">${r.profit_factor == null ? "—" : r.profit_factor.toFixed(2)}</td></tr>`).join("");
    $("cmpTable").innerHTML = `<thead><tr><th>Strategy</th><th class="n">Equity</th><th class="n">Return</th><th class="n">vs B&amp;H</th>
      <th class="n">Max DD</th><th class="n">Sharpe</th><th class="n">Trades</th><th class="n">Win</th><th class="n">PF</th></tr></thead><tbody>${rows}</tbody>`;
    $("cmpTable").querySelectorAll("tr.click").forEach((tr) => tr.onclick = () => selectPf(tr.dataset.pf));
    $("sigNote").textContent = p.significance_note ? `${p.label}: ${p.significance_note}.` : "";
  }

  // ------------------------------------------------------------- equity chart
  async function loadEquity() {
    const d = await api("/api/equity");
    const names = Object.keys(d.series).sort((a, b) => (a === "buy_hold") - (b === "buy_hold"));
    for (const n of names) {
      if (!eqSeries[n]) eqSeries[n] = ec.addLineSeries({ color: COLORS[n] || "#aaa", lineWidth: n === S.pf ? 2 : 1.5,
        lineStyle: n === "buy_hold" ? 2 : 0, priceLineVisible: false, lastValueVisible: true, title: "" });
      eqSeries[n].setData(d.series[n]);
    }
    styleEquity();
    $("eqLegend").innerHTML = names.map((n) => `<span><i class="${n === "buy_hold" ? "dashed" : ""}" style="background:${COLORS[n]}"></i>${esc((LABELS[n] || n).replace(" (benchmark)", "").replace(" (ML + rules)", ""))}</span>`).join("");
    if (names.length && !S.eqFitted) { ec.timeScale().fitContent(); S.eqFitted = true; }
  }
  function styleEquity() { for (const [n, s] of Object.entries(eqSeries)) s.applyOptions({ lineWidth: n === S.pf ? 3 : 1.5 }); }

  // ------------------------------------------------------------- positions
  async function loadPositions() {
    const rows = await api(`/api/positions?portfolio=${S.pf}`);
    $("posHint").textContent = LABELS[S.pf] || S.pf;
    if (!rows.length) { $("posTable").innerHTML = `<tbody><tr><td class="empty">No open positions — the strategy is in cash.</td></tr></tbody>`; return; }
    $("posTable").innerHTML = `<thead><tr><th>Coin</th><th class="n">Size</th><th class="n">Entry</th><th class="n">Price</th>
      <th class="n">Value</th><th class="n">P/L</th><th class="n">Stop</th><th class="n">Target</th></tr></thead><tbody>${rows.map((p) => `
      <tr class="click" data-id="pos-${p.trade_id}"><td><b>${coin(p.symbol)}</b></td><td class="n">${p.qty.toPrecision(5)}</td>
      <td class="n">${price(p.entry_price)}</td><td class="n">${price(p.price)}</td><td class="n">${usd(p.value)}</td>
      <td class="n ${cls(p.pnl)}">${sUsd(p.pnl)} <span class="muted">${pct(p.pnl_pct)}</span></td>
      <td class="n">${p.stop == null ? "—" : price(p.stop)}${p.trailing ? ' <span class="tag">trail</span>' : ""}</td>
      <td class="n">${p.take_profit ? price(p.take_profit) : "—"}</td></tr>
      ${S.expanded.has("pos-" + p.trade_id) ? `<tr class="expl"><td colspan="8"><b>Entry reasoning:</b> ${esc(p.explanation)}</td></tr>` : ""}`).join("")}</tbody>`;
    bindExpand($("posTable"), loadPositions);
  }
  function bindExpand(table, reload) {
    table.querySelectorAll("tr.click[data-id]").forEach((tr) => tr.onclick = () => {
      const id = tr.dataset.id; S.expanded.has(id) ? S.expanded.delete(id) : S.expanded.add(id); reload(); });
  }

  // ------------------------------------------------------------- trades
  async function loadTrades() {
    const rows = await api(`/api/trades?portfolio=${S.pf}&limit=200`);
    if (!rows.length) { $("tradeTable").innerHTML = `<tbody><tr><td class="empty">No closed trades yet.</td></tr></tbody>`; return; }
    const reason = { stop_loss: "stop-loss", trailing_stop: "trailing stop", take_profit: "take-profit", signal: "signal" };
    $("tradeTable").innerHTML = `<thead><tr><th>Closed</th><th>Coin</th><th class="n">Entry</th><th class="n">Exit</th>
      <th class="n">P/L</th><th class="n">P/L %</th><th class="n">Fees</th><th>Exit reason</th><th class="n">Held</th></tr></thead><tbody>${rows.map((t) => `
      <tr class="click" data-id="tr-${t.id}"><td class="muted">${dt(t.exit_time)}</td><td><b>${coin(t.symbol)}</b></td>
      <td class="n">${price(t.entry_price)}</td><td class="n">${price(t.exit_price)}</td>
      <td class="n ${cls(t.pnl)}">${sUsd(t.pnl)}</td><td class="n ${cls(t.pnl_pct)}">${pct(t.pnl_pct)}</td>
      <td class="n muted">${usd(t.entry_fee + t.exit_fee)}</td><td><span class="tag">${reason[t.exit_reason] || esc(t.exit_reason)}</span></td>
      <td class="n muted">${dur((t.exit_time - t.entry_time) / 3.6e6)}</td></tr>
      ${S.expanded.has("tr-" + t.id) ? `<tr class="expl"><td colspan="9"><b>Entry:</b> ${esc(t.entry_explanation)}<br><b>Exit:</b> ${esc(t.exit_explanation)}</td></tr>` : ""}`).join("")}</tbody>`;
    bindExpand($("tradeTable"), loadTrades);
  }

  // ------------------------------------------------------------- decisions feed
  function decisionHtml(d, fresh = false) {
    return `<div class="decision ${fresh ? "fresh" : ""}">
      <div class="row1"><span class="badge ${d.action}">${d.action}</span><b style="color:var(--text)">${coin(d.symbol)}</b>
      <span>${esc(LABELS[d.portfolio] || d.portfolio)}</span><span class="when" title="${dt(d.time)}" data-t="${d.time}">${ago(d.time)}</span></div>
      <p>${esc(d.explanation)}</p>
      ${d.action !== "SELL" || d.confidence < 1 ? `<div class="conf" title="confidence ${(d.confidence * 100).toFixed(0)}%"><i style="width:${(d.confidence * 100).toFixed(0)}%"></i></div>` : ""}</div>`;
  }
  async function loadFeed() {
    const rows = await api(`/api/decisions?limit=60${S.feedAll ? "" : "&portfolio=" + S.pf}`);
    $("feed").innerHTML = rows.length ? rows.map((d) => decisionHtml(d)).join("")
      : `<div class="empty">No trades or blocked signals yet. Decisions are made when each ${S.status?.interval || ""} candle closes; holds are logged but hidden here.</div>`;
  }
  function pushDecision(d) {
    if (!S.feedAll && d.portfolio !== S.pf) return;
    const feed = $("feed"); feed.querySelector(".empty")?.remove();
    feed.insertAdjacentHTML("afterbegin", decisionHtml(d, true));
    while (feed.children.length > 80) feed.lastChild.remove();
  }

  // ------------------------------------------------------------- models
  async function loadModels() {
    const rows = await api("/api/models");
    const act = rows.find((r) => r.active);
    const training = S.status?.training;
    $("retrainBtn").disabled = !!training; $("retrainBtn").textContent = training ? "Training…" : "Retrain now";
    $("modelSummary").innerHTML = [
      ["Active model", act ? `v${act.version}` : "none"],
      ["Validation AUC", act ? act.val_auc.toFixed(3) : "—"],
      ["Edge / signal", act ? pct(act.val_expectancy) : "—"],
      ["Trained", act ? ago(act.created_at) : "—"],
    ].map(([l, v]) => `<div class="ms"><div class="l">${l}</div><div class="v">${v}</div></div>`).join("");
    if (!rows.length) { $("modelTable").innerHTML = `<tbody><tr><td class="empty">No models trained yet.</td></tr></tbody>`; return; }
    $("modelTable").innerHTML = `<thead><tr><th>Ver</th><th>Trained</th><th class="n">AUC</th><th class="n">Edge</th><th class="n">Signals</th><th>Result</th><th></th></tr></thead><tbody>${rows.map((m) => `
      <tr><td><b>v${m.version}</b>${m.active ? ' <span class="tag" style="color:var(--up)">active</span>' : ""}</td><td class="muted">${dt(m.created_at)}</td>
      <td class="n">${m.val_auc.toFixed(3)}</td><td class="n ${cls(m.val_expectancy)}">${pct(m.val_expectancy)}</td><td class="n">${m.val_trades}</td>
      <td class="reason">${m.promoted ? "✓ " : "✗ "}${esc(m.reason)}</td>
      <td>${m.active ? "" : `<button class="btn link" data-v="${m.version}">activate</button>`}</td></tr>`).join("")}</tbody>`;
    $("modelTable").querySelectorAll("button[data-v]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Manually activate model v${b.dataset.v}? This bypasses the promotion check.`)) return;
      await api(`/api/models/${b.dataset.v}/activate`, { method: "POST" }); loadModels();
    });
  }

  // ------------------------------------------------------------- backtests
  async function loadBacktests() {
    S.backtests = await api("/api/backtests");
    const sel = $("btSelect");
    if (!S.backtests.length) {
      sel.innerHTML = "<option>no runs yet</option>";
      $("btTable").innerHTML = `<tbody><tr><td class="empty">Run a backtest to replay the same strategies over past data, with walk-forward model retraining.</td></tr></tbody>`;
      return;
    }
    if (!S.btId || !S.backtests.some((b) => b.id === S.btId)) S.btId = S.backtests[0].id;
    sel.innerHTML = S.backtests.map((b) => `<option value="${b.id}" ${b.id === S.btId ? "selected" : ""}>#${b.id} · ${b.params.days}d · ${new Date(b.created_at).toLocaleDateString()}</option>`).join("");
    showBacktest();
  }
  async function showBacktest() {
    const b = await api(`/api/backtests/${S.btId}`);
    const order = Object.keys(b.results);
    $("btTable").innerHTML = `<thead><tr><th>Strategy</th><th class="n">Return</th><th class="n">vs B&amp;H</th><th class="n">Max DD</th><th class="n">Sharpe</th><th class="n">Trades</th><th class="n">Win</th></tr></thead><tbody>${order.map((n) => { const r = b.results[n]; return `
      <tr><td><span class="swatch" style="background:${COLORS[n]}"></span>${esc(LABELS[n] || n)}</td><td class="n ${cls(r.total_return)}">${pct(r.total_return)}</td>
      <td class="n ${n === "buy_hold" ? "" : cls(r.vs_buy_hold)}">${n === "buy_hold" ? "—" : pct(r.vs_buy_hold)}</td><td class="n">${pct(r.max_drawdown)}</td>
      <td class="n">${r.sharpe == null ? "—" : r.sharpe.toFixed(2)}</td><td class="n">${r.trades}</td><td class="n">${r.win_rate == null ? "—" : pct(r.win_rate, 0, false)}</td></tr>`; }).join("")}</tbody>`;
    btSeries.forEach((s) => bc.removeSeries(s)); btSeries = [];
    for (const n of order) {
      const s = bc.addLineSeries({ color: COLORS[n] || "#aaa", lineWidth: n === "buy_hold" ? 1 : 1.5, lineStyle: n === "buy_hold" ? 2 : 0, priceLineVisible: false, lastValueVisible: false });
      s.setData(b.equity.time.map((t, i) => ({ time: Math.floor(t / 1000), value: b.equity[n][i] })));
      btSeries.push(s);
    }
    bc.timeScale().fitContent();
    const promoted = (b.params.models || []).filter((m) => m.promoted).length;
    $("btNote").textContent = `${dt(b.start_time)} → ${dt(b.end_time)} · ${b.params.interval} · ML retrained ${(b.params.models || []).length}× (${promoted} promoted). Fees and slippage included.`;
  }

  // ------------------------------------------------------------- events + status
  async function loadEvents() {
    const rows = await api("/api/events?limit=80");
    $("eventLog").innerHTML = rows.map(evHtml).join("");
  }
  const evHtml = (e) => `<div class="ev ${e.level}"><span class="t" title="${new Date(e.time).toLocaleString()}">${new Date(e.time).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span><span class="s">${esc(e.source)}</span><span class="m">${esc(e.message)}</span></div>`;

  function renderStatus(st) {
    if (!st) return;
    S.status = { ...(S.status || {}), ...st };
    const pill = $("feedPill");
    const map = { live: ["live", "Live"], polling: ["warn", "Polling (REST)"], reconnecting: ["warn", "Reconnecting"],
                  connecting: ["warn", "Connecting"], starting: ["warn", "Starting"], offline: ["bad", "Offline"] };
    const [k, label] = st.start_error ? ["bad", "Engine error"] : (map[st.feed] || ["warn", st.feed]);
    pill.className = `pill ${k}`;
    $("feedText").textContent = `${label} · ${st.source === "sim" ? "synthetic data" : "Binance"}`;
    pill.title = st.start_error || st.feed_detail || "";
    const up = st.uptime_sec, uptime = up > 86400 ? `${(up / 86400).toFixed(1)}d` : up > 3600 ? `${(up / 3600).toFixed(1)}h` : `${Math.round(up / 60)}m`;
    $("metaBar").textContent = `${st.interval} bars · last decision ${st.last_bar ? dt(st.last_bar + intervalMs(st.interval)) : "—"} · uptime ${uptime} · reconnects ${st.reconnects}`;
    $("sysHint").textContent = st.last_message_age != null ? `last market message ${st.last_message_age}s ago` : "";
    $("btRun").disabled = !!st.backtest_running; $("btRun").textContent = st.backtest_running ? "Running…" : "Run backtest";
  }
  const intervalMs = (i) => ({ m: 6e4, h: 3.6e6, d: 8.64e7 }[i.slice(-1)] * parseInt(i));

  // ------------------------------------------------------------- selection + refresh
  function selectPf(name) {
    S.pf = name; $("pfSelect").value = name; S.expanded.clear();
    loadPortfolios(); loadPositions(); loadTrades(); loadFeed(); loadChart(); styleEquity();
  }
  const refreshLive = throttle(() => { loadPortfolios().catch(() => {}); loadPositions().catch(() => {}); }, 4000);
  async function refreshAll() {
    const tasks = [api("/api/status").then(renderStatus), api("/api/markets").then((m) => { m.forEach((x) => S.markets[x.symbol] = x); renderTabs(); }),
      loadPortfolios(), loadPositions(), loadTrades(), loadFeed(), loadEquity(), loadModels(), loadEvents()];
    await Promise.allSettled(tasks);
  }

  // ------------------------------------------------------------- websocket
  let wsDelay = 1000;
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.onopen = () => { wsDelay = 1000; };
    ws.onmessage = (ev) => {
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "tick": updateTab(m.symbol, m.price); if (m.symbol === S.symbol) applyBar(m.bar); refreshLive(); break;
        case "decision": pushDecision(m.decision);
          if (m.decision.action !== "HOLD" && (S.feedAll || m.decision.portfolio === S.pf))
            toast(`${m.decision.action} ${coin(m.decision.symbol)} · ${LABELS[m.decision.portfolio] || m.decision.portfolio}`, m.decision.action);
          break;
        case "trade": loadPortfolios(); loadPositions(); loadTrades(); if (m.symbol === S.symbol) loadChart(); break;
        case "equity": case "bar": loadEquity(); loadPortfolios(); if (m.type === "bar") { loadChart(); api("/api/markets").then((mk) => mk.forEach((x) => { S.markets[x.symbol] = x; const c = $(`ch-${x.symbol}`); if (c) { c.textContent = pct(x.change_24h) + " 24h"; c.className = "ch " + cls(x.change_24h); } })); } break;
        case "status": renderStatus(m.status); break;
        case "model": S.status && (S.status.training = m.status === "training"); loadModels(); break;
        case "event": $("eventLog").insertAdjacentHTML("afterbegin", evHtml(m.event)); if (m.event.level === "error") toast(m.event.message); break;
        case "backtest": loadBacktests(); api("/api/status").then(renderStatus); break;
      }
    };
    ws.onclose = () => {
      $("feedPill").className = "pill bad"; $("feedText").textContent = "Dashboard disconnected — retrying";
      setTimeout(connect, wsDelay); wsDelay = Math.min(wsDelay * 2, 15000);
    };
  }

  // ------------------------------------------------------------- boot
  async function boot() {
    let st;
    for (;;) { try { st = await api("/api/status"); break; } catch { await new Promise((r) => setTimeout(r, 2000)); } }
    renderStatus(st);
    S.pf = st.primary; S.symbol = st.symbols[0];
    $("pfSelect").innerHTML = [...st.strategies, "buy_hold"].map((n) => `<option value="${n}">${esc(LABELS[n] || n)}</option>`).join("");
    $("pfSelect").value = S.pf;
    $("pfSelect").onchange = (e) => selectPf(e.target.value);
    $("feedAll").onchange = (e) => { S.feedAll = e.target.checked; loadFeed(); };
    $("retrainBtn").onclick = async () => { $("retrainBtn").disabled = true; $("retrainBtn").textContent = "Training…"; await api("/api/models/retrain", { method: "POST" }); };
    $("resumeBtn").onclick = async () => { await api(`/api/portfolios/${S.pf}/resume`, { method: "POST" }); loadPortfolios(); };
    $("btRun").onclick = async () => { const d = parseInt($("btDays").value) || 180; $("btRun").disabled = true; $("btRun").textContent = "Running…";
      const r = await api(`/api/backtests?days=${d}`, { method: "POST" }); toast(r.status === "started" ? `Backtest started (${d} days)` : r.status); };
    $("btSelect").onchange = (e) => { S.btId = parseInt(e.target.value); showBacktest(); };
    await refreshAll();
    await loadChart(true);
    loadBacktests();
    connect();
    setInterval(refreshAll, 60000);
    setInterval(() => document.querySelectorAll(".when[data-t]").forEach((el) => el.textContent = ago(+el.dataset.t)), 15000);
    // engine may still be starting (first backfill); keep checking until data arrives
    const wait = setInterval(async () => { if (S.status?.ready && S.portfolios.length) { clearInterval(wait); return; } await refreshAll(); loadChart(true); }, 5000);
  }
  boot();
})();
