// PaperTrader widget for the Scriptable app (iOS). Supports home-screen (small/medium/large)
// and lock-screen (inline, circular, rectangular) widgets. Loaded by a tiny loader script,
// so updates to this file reach the phone automatically.
const BASE = "https://gusluberadzki-ai.github.io/crypto-trader/";
const UP = new Color("#2ebd85"), DOWN = new Color("#f0616d"), MUTED = new Color("#8a94a6"),
      TEXT = new Color("#e7ebf2"), BG = new Color("#0a0d12"), ACCENT = new Color("#8b9dff");

const pct = (v, d = 1) => {
  if (v == null) return "–";
  const x = Math.abs(v * 100) < Math.pow(10, -d) / 2 ? 0 : v * 100;
  return (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(d) + "%";
};
const colr = (v) => v == null || v === 0 ? MUTED : v > 0 ? UP : DOWN;
const money = (v) => "$" + Math.round(v).toLocaleString("en-US");
const px = (p) => p == null ? "–" : p >= 1000 ? Math.round(p).toLocaleString("en-US") : p.toFixed(p >= 10 ? 1 : 3);
const ago = (ms) => { const m = (Date.now() - ms) / 60000; return m < 60 ? `${Math.round(m)}m ago` : `${(m / 60).toFixed(1)}h ago`; };

async function load() {
  const fm = FileManager.local(), cache = fm.joinPath(fm.cacheDirectory(), "papertrader.json");
  try {
    const d = await new Request(BASE + "widget.json?t=" + Date.now()).loadJSON();
    fm.writeString(cache, JSON.stringify(d));
    return d;
  } catch (e) {
    return fm.fileExists(cache) ? JSON.parse(fm.readString(cache)) : null;
  }
}

function text(stack, str, size, color = TEXT, bold = false, mono = false) {
  const t = stack.addText(str);
  t.font = mono ? (bold ? Font.boldMonospacedSystemFont(size) : Font.regularMonospacedSystemFont(size))
                : (bold ? Font.boldSystemFont(size) : Font.systemFont(size));
  t.textColor = color; t.lineLimit = 1; t.minimumScaleFactor = 0.6;
  return t;
}

function build(d, famOverride) {
  const fam = famOverride || config.widgetFamily || "medium";
  const w = new ListWidget();
  w.url = d ? d.url : BASE;
  w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);
  if (!d) { w.addText("PaperTrader: no data yet"); return w; }
  const main = d.portfolios.find((p) => p.name === d.primary) || d.portfolios[0];
  const best = [...d.portfolios].sort((a, b) => b.return - a.return)[0];

  const dir = (v) => v > 0.00005 ? "▲" : v < -0.00005 ? "▼" : "•";
  const k = (p) => p >= 10000 ? (p / 1000).toFixed(1) + "k" : px(p);
  if (fam === "accessoryInline") {
    w.addText(`${dir(main.return)} ${pct(main.return, 2)}  ·  B&H ${pct(d.buy_hold, 2)}`);
    return w;
  }
  if (fam === "accessoryCircular") {
    const s = w.addStack(); s.layoutVertically();
    const top = s.addStack(); top.addSpacer();
    const img = top.addImage(SFSymbol.named(main.return >= d.buy_hold ? "chart.line.uptrend.xyaxis" : "chart.line.downtrend.xyaxis").image);
    img.imageSize = new Size(16, 16); img.tintColor = Color.white(); top.addSpacer();
    s.addSpacer(2);
    const mid = s.addStack(); mid.addSpacer(); text(mid, pct(main.return, 1), 13, Color.white(), true, true); mid.addSpacer();
    return w;
  }
  if (fam === "accessoryRectangular") {
    const head = w.addStack(); head.centerAlignContent();
    text(head, "PAPERTRADER", 10, new Color("#ffffff", 0.7), true);
    head.addSpacer();
    text(head, ago(d.updated), 10, new Color("#ffffff", 0.6));
    const mid = w.addStack(); mid.bottomAlignContent();
    text(mid, money(main.equity), 19, Color.white(), true, true);
    mid.addSpacer(6);
    text(mid, `${dir(main.return)} ${pct(main.return, 2)}`, 12, Color.white(), true, true);
    text(w, `vs B&H ${pct(d.buy_hold, 2)} · BTC ${k(d.prices[0].price)}`, 11, new Color("#ffffff", 0.75), false, true);
    return w;
  }

  w.backgroundColor = BG;
  w.setPadding(12, 14, 12, 14);
  const head = w.addStack(); head.centerAlignContent();
  text(head, "PaperTrader", 12, ACCENT, true); head.addSpacer();
  text(head, ago(d.updated), 10, MUTED);
  w.addSpacer(6);
  text(w, money(main.equity), fam === "small" ? 22 : 24, TEXT, true, true);
  const sub = w.addStack();
  text(sub, pct(main.return, 2), 12, colr(main.return), true, true);
  text(sub, `  vs B&H ${pct(d.buy_hold, 2)}`, 12, MUTED, false, true);
  if (fam === "small") {
    w.addSpacer();
    text(w, `${main.label} · ${main.open} open`, 10, MUTED);
    return w;
  }
  w.addSpacer(8);
  const row = w.addStack();
  const col = (parent, width, items, render, alignRight) => {
    const c = parent.addStack(); c.layoutVertically(); c.size = new Size(width, 0);
    for (const it of items) { const r = c.addStack(); if (alignRight) r.addSpacer(); render(r, it); if (!alignRight) r.addSpacer(); }
  };
  const short = { ensemble: "Ensemble", trend: "Trend", mean_reversion: "Mean rev", ml: "ML" };
  col(row, 62, d.portfolios, (r, p) => text(r, short[p.name] || p.label, 10, MUTED), false);
  col(row, 44, d.portfolios, (r, p) => text(r, pct(p.return), 10, colr(p.return), false, true), true);
  row.addSpacer();
  col(row, 30, d.prices, (r, p) => text(r, p.symbol, 10, MUTED), false);
  col(row, 52, d.prices, (r, p) => text(r, px(p.price), 10, TEXT, false, true), true);
  col(row, 44, d.prices, (r, p) => text(r, pct(p.change), 10, colr(p.change), false, true), true);
  w.addSpacer();
  return w;
}

const data = await load();
const widget = build(data);
if (config.runsInWidget) Script.setWidget(widget);
else {
  // In the app: let you preview any size
  const pick = new Alert(); pick.title = "Preview size";
  ["Medium", "Small", "Lock screen (rectangle)", "Lock screen (round)"].forEach((o) => pick.addAction(o));
  const i = await pick.presentSheet();
  const fam = ["medium", "small", "accessoryRectangular", "accessoryCircular"][i] || "medium";
  const wv = build(data, fam);
  if (fam === "small") await wv.presentSmall();
  else if (fam === "accessoryRectangular" && wv.presentAccessoryRectangular) await wv.presentAccessoryRectangular();
  else if (fam === "accessoryCircular" && wv.presentAccessoryCircular) await wv.presentAccessoryCircular();
  else await wv.presentMedium();
}
Script.complete();
