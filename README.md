# Angel One Paper Trading Agent

This is a cautious Angel One SmartAPI trading agent. Release 5 can automatically fill and manage simulated paper orders, but it cannot submit live Angel One orders.

## What It Does

- Loads strategy, risk, market-data, and broker settings from `config.example.yaml`
- Analyzes OHLCV candles with EMA, RSI, VWAP, MACD, ATR, and support/resistance
- Scores economic and geopolitical news sentiment with a transparent keyword model
- Combines technical trend, news sentiment, and risk checks into a trade decision
- Runs in paper mode by default
- Provides read-only Angel One historical candle retrieval when explicitly configured
- Provides a broker adapter interface and an Angel One adapter stub that blocks every order submission in Release 2
- Logs every decision as JSON lines
- Records the indicator values and point-by-point strategy explanation for each decision
- Adds optional, read-only NSE bulk-deal context with a maximum 10-point confidence contribution
- Serves a private read-only dashboard through a localhost-only FastAPI process and authenticated HTTPS proxy
- Optionally ranks a controlled universe by signal confidence and recent liquidity, then sends only the top candidate through the paper-trading safety gates
- Persists simulated paper entries and exits with duplicate protection, fees, slippage, stop-loss, target, and time-based exits

## Important Safety Notes

- This is not financial advice.
- Use paper trading first.
- Release 5 rejects live trading and keeps `TRADING_MODE=paper`.
- `LIVE_TRADING_ENABLED` defaults to `false`; setting it to `true` always fails closed.
- `KILL_SWITCH_ACTIVE` defaults to `true` and blocks all order execution while active.
- `AUTO_PAPER_TRADING_ENABLED` defaults to `false`, providing a separate environment-level opt-in.
- High-impact news protection remains mandatory.
- Credentials must come from environment variables or a secret manager, not tracked config files.
- Always use stop-loss, max daily loss, and the kill switch.

## Setup

```powershell
cd "C:\Users\Lokesh anand\OneDrive\Documents\magic prompts genrator\trading_agent"
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy config.example.yaml config.yaml
```

Edit `config.yaml` with symbols and non-secret settings only. Do not put credentials in YAML.

## Release 2 Read-Only Market Data

`market_data.provider` defaults to `demo`. To fetch read-only Angel One historical candles, set `market_data.provider` to `angel_one` in local configuration and provide these environment variables:

```powershell
$env:ANGEL_ONE_API_KEY = "<from secret manager>"
$env:ANGEL_ONE_CLIENT_CODE = "<from secret manager>"
$env:ANGEL_ONE_PIN = "<from secret manager>"
$env:ANGEL_ONE_TOTP_SECRET = "<from secret manager>"
```

The Angel One integration only authenticates and calls historical candle data. This release cannot submit, modify, cancel, or otherwise place live orders.

## Release 3 Strategy Explanation

Every new decision includes a structured `signal.strategy` object with:

- Strategy name, version, timeframe, parameters, and candle count
- EMA fast, EMA slow, EMA trend, VWAP, RSI, MACD, MACD signal, ATR, and close values
- A signed point contribution for EMA alignment, VWAP, RSI, MACD, news, and bulk deals
- Bullish points, bearish points, and the raw scoring edge

The strategy remains a transparent deterministic rules engine. It is not a machine-learning model.

## Release 3 NSE Bulk Deals

Bulk-deal analysis is disabled by default. To enable the read-only NSE adapter, use this non-secret config:

```yaml
bulk_deals:
  enabled: true
  provider: nse
  max_age_days: 7
  max_confidence_points: 10
  min_imbalance_ratio: 0.2
  timeout_seconds: 8
```

The adapter only performs HTTP GET requests to NSE's public large-deal publication. Matching uses the cash-equity symbol, so `SBIN-EQ` maps to `SBIN`. Stale, missing, or unavailable data contributes zero points. Bulk-deal context cannot bypass the confidence, risk, news, manual-approval, kill-switch, or paper-only controls.

Official source: <https://www.nseindia.com/market-data/large-deals>

## Release 3 Private Dashboard

Run locally for development:

```powershell
python run_dashboard.py --host 127.0.0.1 --port 8000 --decision-log logs/decisions.jsonl
```

The dashboard exposes only GET routes:

- `/` — browser dashboard
- `/api/health` — paper-mode safety state
- `/api/latest` — latest sanitized decision
- `/api/decisions?limit=50` — sanitized history

The server refuses `0.0.0.0` and other public bind addresses. In production, keep it on `127.0.0.1:8000` and publish it only through Caddy HTTPS with `basic_auth`. The tracked templates are:

- `deploy/tradebot-dashboard.service`
- `deploy/Caddyfile.example`

Generate the Caddy password hash interactively with `caddy hash-password`; never commit the password or resulting production configuration. The SmartAPI callback path remains outside dashboard authentication, while all dashboard routes require authentication.

## Release 4 Automatic Scanner

The scanner is disabled by default. It compares only explicitly approved symbols under `trading.symbols`; it does not discover or trade arbitrary instruments. Enable it after adding at least two valid Angel One instruments:

```yaml
scanner:
  enabled: true
  max_candidates: 20
  min_average_volume: 100000
```

Directional candidates with sufficient recent average volume are ranked by confidence, followed by average traded turnover. The selected symbol and full ranking are written to the decision log and dashboard. Unavailable symbols are skipped with sanitized error text. Selection cannot bypass the confidence threshold, risk checks, high-impact-news block, manual approval, kill switch, or paper-only broker.

NIFTY 50 (`99926000`) and NIFTY BANK (`99926009`) spot indices are supported as read-only underlying signals on NSE. Because a spot index is not itself traded, the scanner excludes VWAP and the equity-volume threshold for those two signals.

Release 4 can use those signals to discover current NFO contracts from Angel One's instrument master. Contract tokens are resolved at runtime rather than hard-coded because they change with expiry:

```yaml
derivatives:
  enabled: true
  instruments: [futures, options]
  option_buying_only: true
  option_strikes: 1
  minimum_expiry_days: 1
  max_expiry_days: 45
  max_contracts: 6
  timeframe: FIVE_MINUTE
  timeout_seconds: 10
```

The nearest permitted index future and nearest-expiry directional option are evaluated. The default one-day minimum excludes same-day-expiry contracts. Bullish underlying signals consider call buying; bearish signals consider put buying. Option selling is rejected by configuration validation. Lot size, expiry, strike, trading symbol, and token come from the current instrument master. F&O discovery and candle retrieval are read-only, and all resulting decisions still pass through the existing paper-only safety gates.

Angel One candle requests are serialized with a configurable minimum interval and bounded retry backoff for `AB1021` rate limits. SmartAPI SDK logging is suppressed around authenticated calls because upstream error logging may include sensitive request headers. Application exceptions remain sanitized.

## Release 4 Walk-Forward Backtesting

Backtesting can be enabled for the selected cash, index, futures, or long-option candidate:

```yaml
backtesting:
  enabled: true
  minimum_candles: 80
  holding_bars: 12
  round_trip_cost_bps: 10
```

The engine evaluates each signal using only candles available at that point, prevents overlapping simulated trades, deducts configured round-trip costs, and uses a conservative stop-first result when a candle touches both stop and target. Current news and bulk-deal information are forced to neutral during historical evaluation to avoid look-ahead contamination. The dashboard reports trade count, win rate, net compounded return, maximum drawdown, average trade, and profit factor.

Backtest results are research estimates, not guarantees. They do not model every tax, brokerage charge, spread, liquidity constraint, gap, rejection, or execution delay. Expired F&O history may also be limited by the broker's available historical data.

## Release 5 Automatic Paper Execution

Automatic paper execution is disabled by default and requires all safety gates to agree. Configure the simulator without credentials:

```yaml
paper_execution:
  enabled: true
  initial_balance: 100000
  max_open_positions: 2
  slippage_bps: 5
  fee_bps: 10
  max_holding_minutes: 120
  market_hours_only: true
  max_candle_age_minutes: 10
  state_file: /opt/tradebot/logs/paper_portfolio.json
```

Then explicitly set `AUTO_PAPER_TRADING_ENABLED=true`, `KILL_SWITCH_ACTIVE=false`, and `trading.require_manual_approval=false`. `LIVE_TRADING_ENABLED` must remain `false`.

An entry is filled only when the scanner candidate is eligible, confidence meets the threshold, risk approves a full F&O lot, high-impact news is absent, daily limits allow another trade, automatic paper execution is enabled, and the kill switch is inactive. Long options can only be bought; option selling is rejected again inside the paper broker.

The state file is written atomically with mode `0600`. It records open positions, closed trades, simulated fees, slippage, and `PAPER-` order identifiers. Repeated scans of the same candle cannot create duplicate orders, and a second position in the same contract is blocked. Existing positions are checked against later candles for stop-loss, target, gap, and configured time exits before a new scan.

This is a simulation. Paper fills do not guarantee comparable live-market fills, liquidity, spreads, or costs.

## Run Paper Trading Demo

```powershell
python run_agent.py --config config.example.yaml --demo
```

## Run With Your Config

```powershell
python run_agent.py --config config.yaml
```

## Safety Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest -v
```

## Release 5 Execution Guard

Order execution only reaches the paper broker when all of these are true:

- `TRADING_MODE` resolves to `paper`
- `LIVE_TRADING_ENABLED` is `false`
- `paper_execution.enabled` is `true`
- `AUTO_PAPER_TRADING_ENABLED` is `true`
- `KILL_SWITCH_ACTIVE` is `false`
- `trading.require_manual_approval` is `false`
- Risk manager approves the trade
- Strategy confidence is at or above the configured threshold
- The candidate is eligible and a full F&O lot fits within risk limits

The default config is deliberately conservative: paper mode, automatic paper execution disabled, manual approval required, mandatory high-impact news protection, and the kill switch active. Source scans and tests ensure no Angel One order, modify, cancel, or GTT endpoint is present.
