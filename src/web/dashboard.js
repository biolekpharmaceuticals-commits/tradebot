"use strict";

const byId = (id) => document.getElementById(id);
const text = (id, value) => { byId(id).textContent = value ?? "—"; };
const number = (value, digits = 2) => Number.isFinite(Number(value))
  ? Number(value).toLocaleString("en-IN", { maximumFractionDigits: digits })
  : "—";

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
  const safe = record.live_trading_enabled === false && record.executed === false;
  byId("safetyBanner").className = `safety-banner ${safe ? "" : "warning"}`;
  text(
    "safetyText",
    `Kill switch ${record.kill_switch_active ? "active" : "inactive"}; live trading ${record.live_trading_enabled ? "enabled" : "disabled"}; executed ${record.executed ? "yes" : "no"}.`,
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

  text("reason", signal.reason);
  text("riskReason", `${risk.approved ? "Approved" : "Blocked"}: ${risk.reason || "No reason"}`);
  text("newsRisk", `${news.risk_level || "Unknown"} · score ${number(news.score, 0)} · ${news.explanation || ""}`);
  text("execution", record.executed ? "Paper order recorded" : "No order executed");
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

refresh();
setInterval(refresh, 60000);
