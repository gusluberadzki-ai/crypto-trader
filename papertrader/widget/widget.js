// PaperTrader widget for the Scriptable app (iOS). Supports home-screen (small/medium/large)
// and lock-screen (inline, circular, rectangular) widgets. Loaded by a tiny loader script,
// so updates to this file reach the phone automatically.
const BASE = "https://gusluberadzki-ai.github.io/crypto-trader/";
const UP = new Color("#2ebd85"), DOWN = new Color("#f0616d"), MUTED = new Color("#8a94a6"),
      TEXT = new Color("#e7ebf2"), BG = new Color("#0a0d12"), ACCENT = new Color("#8b9dff");

const pct = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%";
const col = (v) => v == null || v === 0 ? MUTED : v > 0 ? UP : DOWN;
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

function build(d) {
  const fam = config.widgetFamily || "medium";
  const w = new ListWidget();
  w.url = d ? d.url : BASE;
  w.refreshAfterDate = new Date(Date.now() + 30 * 60 * 1000);
  if (!d) { w.addText("PaperTrader: no data yet"); return w; }
  const main = d.portfolios.find((p) => p.name === d.primary) || d.portfolios[0];
  const best = [...d.portfolios].sort((a, b) => b.return - a.return)[0];

  if (fam === "accessoryInline") { w.addText(`📈 ${pct(main.return)} · B&H ${pct(d.buy_hold)}`); return w; }
  if (fam === "accessoryCircular") {
    w.addAccessoryWidgetBackground = true;
    const s = w.addStack(); s.layoutVertically(); s.centerAlignContent();
    text(s, "PT", 10, Color.white(), true); text(s, pct(main.return, 1), 12, Color.white(), true);
    return w;
  }
  if (fam === "accessoryRectangular") {
    w.addAccessoryWidgetBackground = true;
    text(w, `PaperTrader ${pct(main.return)}`, 13, Color.white(), true);
    text(w, `Buy&hold ${pct(d.buy_hold)} · best ${best.label.split(" ")[0]} ${pct(best.return)}`, 11, Color.white());
    text(w, d.prices.map((p) => `${p.symbol} ${px(p.price)}`).join("  "), 10, Color.white());
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
  text(sub, pct(main.return, 2), 12, col(main.return), true, true);
  text(sub, `  vs B&H ${pct(d.buy_hold, 2)}`, 12, MUTED, false, true);
  if (fam === "small") {
    w.addSpacer();
    text(w, `${main.label} · ${main.open} open`, 10, MUTED);
    return w;
  }
  w.addSpacer(8);
  const row = w.addStack();
  const left = row.addStack(); left.layoutVertically();
  for (const p of d.portfolios) {
    const r = left.addStack();
    text(r, p.label.padEnd(15).slice(0, 15), 10, MUTED, false, true);
    text(r, pct(p.return).padStart(7), 10, col(p.return), false, true);
  }
  row.addSpacer();
  const right = row.addStack(); right.layoutVertically();
  for (const p of d.prices) {
    const r = right.addStack();
    text(r, p.symbol.padEnd(4), 10, MUTED, false, true);
    text(r, px(p.price).padStart(7), 10, TEXT, false, true);
    text(r, " " + pct(p.change), 10, col(p.change), false, true);
  }
  w.addSpacer();
  return w;
}

const data = await load();
const widget = build(data);
if (config.runsInWidget) Script.setWidget(widget);
else await widget.presentMedium();
Script.complete();
