"use strict";

const byId = (id) => document.getElementById(id);
const text = (id, value) => { byId(id).textContent = value ?? "—"; };
const number = (value, digits = 2) => Number.isFinite(Number(value))
  ? Number(value).toLocaleString("en-IN", { maximumFractionDigits: digits })
  : "—";
let lastMarketPayload = null;

function cell(value, className = "") {
  const td = document.createElement("td");
  td.textContent = value ?? "—";
  if (className) td.className = className;
  return td;
}

function replaceRows(id, rows) {
  const body = byId(id);
  body.replaceChildren(...rows);
}

function renderLatest(record) {
  const signal = record.signal || {};
  const strategy = signal.strategy || {};
  const risk = record.risk || {};
  const news = record.news || {};
  const bulk = record.bulk_deals || {};
  const scanner = record.scanner || {};
  const backtest = record.backtest || {};
  const portfolio = record.paper_portfolio || {};
  const decision = signal.decision || "NO DATA";

  text("decision", decision);
  text("symbol", record.symbol || "No symbol");
  text("confidence", `${number(signal.confidence, 0)}%`);
  byId("confidenceBar").style.width = `${Math.max(0, Math.min(100, Number(signal.confidence) || 0))}%`;
  text("entry", number(signal.entry_price));
  text("marketBias", signal.market_bias || "—");
  text("stopTarget", `${number(signal.stop_loss)} / ${number(signal.target)}`);
  text("rewardRisk", `Reward/risk ${number(signal.reward_risk)}`);
  text("updatedAt", record.timestamp ? `Updated ${new Date(record.timestamp).toLocaleString("en-IN")}` : "No timestamp");

  byId("decision").className = `decision-${decision.toLowerCase()}`;
  text("modeBadge", `${String(record.mode || "paper").toUpperCase()} MODE`);
  const safe = record.live_trading_enabled === false && String(record.mode || "paper") === "paper";
  byId("safetyBanner").className = `safety-banner ${safe ? "" : "warning"}`;
  text(
    "safetyText",
    `Kill switch ${record.kill_switch_active ? "active" : "inactive"}; live trading ${record.live_trading_enabled ? "enabled" : "disabled"}; automatic paper execution ${record.auto_paper_trading_enabled ? "enabled" : "disabled"}; paper order filled ${record.executed ? "yes" : "no"}.`,
  );

  text("strategyName", `${strategy.name || "Strategy not recorded"} · ${strategy.type || ""}`);
  text("strategyVersion", strategy.version ? `v${strategy.version}` : "—");
  renderIndicators(strategy.indicators || {});
  renderFactors(strategy.factors || []);

  text("bulkStatus", String(bulk.status || "unknown").toUpperCase());
  text("bulkDirection", bulk.direction || "Neutral");
  text("bulkScore", `${Number(bulk.score) > 0 ? "+" : ""}${number(bulk.score, 0)}`);
  text("bulkBuy", number(bulk.buy_quantity, 0));
  text("bulkSell", number(bulk.sell_quantity, 0));
  text("bulkExplanation", `${bulk.explanation || "No bulk-deal data"}${bulk.latest_date ? ` · Published ${bulk.latest_date}` : ""}`);
  renderBulkDeals(bulk.deals || []);
  renderScanner(scanner);
  renderBacktest(backtest);
  renderPaperPortfolio(record, portfolio);

  text("reason", signal.reason);
  text("riskReason", `${risk.approved ? "Approved" : "Blocked"}: ${risk.reason || "No reason"}`);
  text("newsRisk", `${news.risk_level || "Unknown"} · score ${number(news.score, 0)} · ${news.explanation || ""}`);
  text(
    "execution",
    record.executed
      ? `${record.order?.message || "Paper order filled"} · ${record.order?.order_id || ""}`
      : (record.execution_blockers || []).join("; ") || record.order?.message || "No paper order executed",
  );
}

function renderMarket(payload) {
  lastMarketPayload = payload;
  const status = String(payload.status || "unavailable").toUpperCase();
  text("marketFeedStatus", status);
  byId("marketFeedStatus").className = `badge ${payload.status === "available" ? "safe" : ""}`;
  const errors = Array.isArray(payload.errors) ? payload.errors : [];
  text(
    "marketFeedExplanation",
    payload.status === "available"
      ? `Read-only ${String(payload.timeframe || "ONE_MINUTE").replaceAll("_", " ").toLowerCase()} candles · refreshes every ${number(payload.refresh_seconds, 0)} seconds.`
      : errors.map((item) => `${item.symbol}: ${item.reason}`).join("; ") || "Live index candles are not configured.",
  );

  const indices = Array.isArray(payload.indices) ? payload.indices : [];
  renderIndexCard("nifty50", indices.find((item) => item.symbol === "NIFTY 50"));
  renderIndexCard("bankNifty", indices.find((item) => item.symbol === "NIFTY BANK"));
}

function renderIndexCard(prefix, market) {
  if (!market) {
    text(`${prefix}Price`, "—");
    text(`${prefix}Change`, "Unavailable");
    text(`${prefix}Action`, "No current candles");
    drawCandles(byId(`${prefix}Chart`), []);
    return;
  }

  const change = Number(market.change) || 0;
  const changePct = Number(market.change_pct) || 0;
  text(`${prefix}Price`, number(market.price));
  text(`${prefix}Change`, `${change >= 0 ? "+" : ""}${number(change)} (${changePct >= 0 ? "+" : ""}${number(changePct)}%)`);
  byId(`${prefix}Change`).className = change >= 0 ? "positive" : "negative";
  text(`${prefix}Action`, market.price_action || "—");
  text(`${prefix}Open`, number(market.open));
  text(`${prefix}High`, number(market.high));
  text(`${prefix}Low`, number(market.low));
  text(`${prefix}Previous`, number(market.previous_close));
  text(`${prefix}Time`, market.timestamp ? `Latest candle ${new Date(market.timestamp).toLocaleString("en-IN")}` : "No timestamp");
  drawCandles(byId(`${prefix}Chart`), Array.isArray(market.candles) ? market.candles : []);
}

function drawCandles(canvas, candles) {
  const width = Math.max(320, Math.floor(canvas.clientWidth || 640));
  const height = 300;
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.scale(ratio, ratio);
  context.clearRect(0, 0, width, height);

  const data = candles.slice(-60);
  if (!data.length) {
    context.fillStyle = "#90a59d";
    context.font = "13px system-ui";
    context.textAlign = "center";
    context.fillText("Waiting for one-minute candles", width / 2, height / 2);
    return;
  }

  const padding = { top: 14, right: 62, bottom: 28, left: 8 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const lows = data.map((item) => Number(item.low));
  const highs = data.map((item) => Number(item.high));
  let minimum = Math.min(...lows);
  let maximum = Math.max(...highs);
  const priceRange = Math.max(maximum - minimum, Math.abs(maximum) * 0.0005, 1);
  minimum -= priceRange * 0.08;
  maximum += priceRange * 0.08;
  const y = (price) => padding.top + (maximum - Number(price)) / (maximum - minimum) * plotHeight;

  context.strokeStyle = "rgba(144,165,157,.14)";
  context.fillStyle = "#90a59d";
  context.font = "10px system-ui";
  context.textAlign = "left";
  for (let line = 0; line <= 4; line += 1) {
    const gridY = padding.top + plotHeight * line / 4;
    context.beginPath();
    context.moveTo(padding.left, gridY);
    context.lineTo(width - padding.right, gridY);
    context.stroke();
    const label = maximum - (maximum - minimum) * line / 4;
    context.fillText(number(label), width - padding.right + 8, gridY + 3);
  }

  const slot = plotWidth / data.length;
  const bodyWidth = Math.max(2, Math.min(9, slot * 0.62));
  data.forEach((item, index) => {
    const center = padding.left + slot * index + slot / 2;
    const openY = y(item.open);
    const closeY = y(item.close);
    const rising = Number(item.close) >= Number(item.open);
    const color = rising ? "#55e6a5" : "#ff7b83";
    context.strokeStyle = color;
    context.fillStyle = color;
    context.beginPath();
    context.moveTo(center, y(item.high));
    context.lineTo(center, y(item.low));
    context.stroke();
    context.fillRect(center - bodyWidth / 2, Math.min(openY, closeY), bodyWidth, Math.max(1, Math.abs(closeY - openY)));
  });

  const latest = data[data.length - 1];
  context.strokeStyle = Number(latest.close) >= Number(latest.open) ? "rgba(85,230,165,.65)" : "rgba(255,123,131,.65)";
  context.setLineDash([4, 4]);
  context.beginPath();
  context.moveTo(padding.left, y(latest.close));
  context.lineTo(width - padding.right, y(latest.close));
  context.stroke();
  context.setLineDash([]);

  const timeIndices = [0, Math.floor((data.length - 1) / 2), data.length - 1];
  context.fillStyle = "#90a59d";
  context.textAlign = "center";
  timeIndices.forEach((index) => {
    const date = new Date(data[index].timestamp);
    const label = date.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" });
    context.fillText(label, padding.left + slot * index + slot / 2, height - 8);
  });
}

function renderPaperPortfolio(record, portfolio) {
  const automatic = record.paper_execution_configured && record.auto_paper_trading_enabled;
  text("paperStatus", automatic ? (record.kill_switch_active ? "PAUSED" : "AUTOMATIC") : "DISABLED");
  text("paperBalance", `₹${number(portfolio.paper_balance)}`);
  text("paperPnl", `₹${number(portfolio.realized_pnl)}`);
  text("paperOpenCount", number(portfolio.open_position_count, 0));
  text("paperClosedCount", number(portfolio.closed_trade_count, 0));
  text(
    "paperExplanation",
    automatic
      ? `Paper fills use configured slippage and fees. ${record.kill_switch_active ? "New entries are paused by the kill switch." : "Eligible signals can be filled automatically."}`
      : "Automatic paper execution requires both configuration and environment opt-in.",
  );

  const positions = Array.isArray(portfolio.open_positions) ? portfolio.open_positions : [];
  const positionRows = positions.map((position) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(position.symbol), cell(position.side, position.side === "BUY" ? "positive" : "negative"),
      cell(number(position.quantity, 0)), cell(number(position.entry_price)), cell(number(position.stop_loss)),
      cell(number(position.target)), cell(position.opened_at ? new Date(position.opened_at).toLocaleString("en-IN") : "—"),
    );
    return tr;
  });
  replaceRows("paperPositionRows", positionRows.length ? positionRows : [emptyRow(7, "No open paper positions")]);

  const orders = Array.isArray(portfolio.recent_orders) ? portfolio.recent_orders : [];
  const orderRows = orders.map((order) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(order.timestamp ? new Date(order.timestamp).toLocaleString("en-IN") : "—"),
      cell(order.order_id), cell(order.kind), cell(order.symbol),
      cell(order.side, order.side === "BUY" ? "positive" : "negative"), cell(number(order.quantity, 0)),
      cell(number(order.fill_price)), cell(order.status),
    );
    return tr;
  });
  replaceRows("paperOrderRows", orderRows.length ? orderRows : [emptyRow(8, "No paper orders recorded")]);
}

function renderBacktest(backtest) {
  text("backtestStatus", String(backtest.status || "unknown").toUpperCase());
  text("backtestTrades", number(backtest.trades, 0));
  text("backtestWinRate", backtest.status === "complete" ? `${number(backtest.win_rate_pct)}%` : "—");
  text("backtestReturn", backtest.status === "complete" ? `${number(backtest.net_return_pct)}%` : "—");
  text("backtestDrawdown", backtest.status === "complete" ? `${number(backtest.max_drawdown_pct)}%` : "—");
  text(
    "backtestExplanation",
    backtest.status === "complete"
      ? `${backtest.method}. Costs ${number(backtest.round_trip_cost_bps)} bps; ${backtest.news_assumption}. Historical performance does not predict future results.`
      : backtest.status === "insufficient_data"
        ? `Insufficient candles: ${number(backtest.candles, 0)} available, ${number(backtest.minimum_candles, 0)} required.`
        : "Backtesting was disabled for this decision.",
  );
}

function renderScanner(scanner) {
  text("scannerStatus", scanner.enabled ? "ACTIVE" : "DISABLED");
  text(
    "scannerExplanation",
    scanner.enabled
      ? `${scanner.selection_reason || "Ranked configured candidates"}. Selected ${scanner.selected_symbol || "none"}; evaluated ${number(scanner.evaluated, 0)}.`
      : "Scanner was disabled for this decision.",
  );
  const ranking = Array.isArray(scanner.ranking) ? scanner.ranking : [];
  const rows = ranking.map((candidate) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(number(candidate.rank, 0)),
      cell(candidate.symbol),
      cell(candidate.derivative_type || candidate.instrument_type),
      cell(candidate.expiry),
      cell(number(candidate.lot_size, 0)),
      cell(candidate.decision),
      cell(`${number(candidate.confidence, 0)}%`),
      cell(number(candidate.average_volume, 0)),
      cell(candidate.eligible ? "Eligible" : candidate.reason, candidate.eligible ? "positive" : "negative"),
    );
    return tr;
  });
  replaceRows("scannerRows", rows.length ? rows : [emptyRow(9, "No scanner ranking recorded")]);
}

function renderIndicators(indicators) {
  const grid = byId("indicatorGrid");
  const labels = {
    close: "Close", ema_fast: "EMA fast", ema_slow: "EMA slow", ema_trend: "EMA trend",
    vwap: "VWAP", rsi: "RSI", macd: "MACD", macd_signal: "MACD signal", atr: "ATR",
  };
  const cards = Object.entries(labels).map(([key, label]) => {
    const item = document.createElement("div");
    const title = document.createElement("span");
    const value = document.createElement("strong");
    title.textContent = label;
    value.textContent = number(indicators[key], 4);
    item.append(title, value);
    return item;
  });
  grid.replaceChildren(...cards);
}

function renderFactors(factors) {
  const rows = factors.map((factor) => {
    const tr = document.createElement("tr");
    const points = Number(factor.points) || 0;
    tr.append(
      cell(factor.name),
      cell(factor.direction, `tone-${String(factor.direction || "neutral").toLowerCase()}`),
      cell(`${points > 0 ? "+" : ""}${points}`, points > 0 ? "positive" : points < 0 ? "negative" : ""),
      cell(factor.explanation),
    );
    return tr;
  });
  replaceRows("factorRows", rows.length ? rows : [emptyRow(4, "No factor breakdown recorded")]);
}

function renderBulkDeals(deals) {
  const rows = deals.map((deal) => {
    const tr = document.createElement("tr");
    tr.append(
      cell(deal.date), cell(deal.client_name), cell(deal.side, deal.side === "BUY" ? "positive" : "negative"),
      cell(number(deal.quantity, 0)), cell(number(deal.average_price)),
    );
    return tr;
  });
  replaceRows("bulkRows", rows.length ? rows : [emptyRow(5, "No matching published bulk deals")]);
}

function renderHistory(records) {
  const rows = records.map((record) => {
    const signal = record.signal || {};
    const tr = document.createElement("tr");
    tr.append(
      cell(record.timestamp ? new Date(record.timestamp).toLocaleString("en-IN") : "—"),
      cell(record.symbol), cell(signal.decision), cell(`${number(signal.confidence, 0)}%`),
      cell(record.executed ? "Yes" : "No"),
    );
    return tr;
  });
  replaceRows("historyRows", rows.length ? rows : [emptyRow(5, "No decisions recorded yet")]);
}

function emptyRow(columns, message) {
  const tr = document.createElement("tr");
  const td = cell(message, "empty");
  td.colSpan = columns;
  tr.append(td);
  return tr;
}

async function refresh() {
  try {
    const response = await fetch("/api/decisions?limit=25", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const records = payload.decisions || [];
    if (records.length) renderLatest(records[0]);
    renderHistory(records);
  } catch (error) {
    text("updatedAt", "Dashboard data unavailable");
    text("safetyText", "Could not read the local decision log. Trading execution remains unchanged.");
  }
}

async function refreshMarket() {
  try {
    const response = await fetch("/api/market", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    renderMarket(await response.json());
  } catch (error) {
    text("marketFeedStatus", "UNAVAILABLE");
    text("marketFeedExplanation", "Could not read the live market feed. Trading execution remains unchanged.");
  }
}

refresh();
refreshMarket();
setInterval(refresh, 60000);
setInterval(refreshMarket, 15000);
let resizeTimer;
window.addEventListener("resize", () => {
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {
    if (lastMarketPayload) renderMarket(lastMarketPayload);
  }, 120);
});
