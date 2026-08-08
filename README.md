# Angel One Auto Trading Agent MVP

This is a cautious starter framework for an Angel One SmartAPI trading agent. Release 2 is paper-trading-only and cannot submit live Angel One orders.

## What It Does

- Loads strategy, risk, market-data, and broker settings from `config.example.yaml`
- Analyzes OHLCV candles with EMA, RSI, VWAP, MACD, ATR, and support/resistance
- Scores economic and geopolitical news sentiment with a transparent keyword model
- Combines technical trend, news sentiment, and risk checks into a trade decision
- Runs in paper mode by default
- Provides read-only Angel One historical candle retrieval when explicitly configured
- Provides a broker adapter interface and an Angel One adapter stub that blocks every order submission in Release 2
- Logs every decision as JSON lines

## Important Safety Notes

- This is not financial advice.
- Use paper trading first.
- Release 2 rejects live trading and keeps `TRADING_MODE=paper` by default.
- `LIVE_TRADING_ENABLED` defaults to `false`; setting it to `true` fails closed in Release 2.
- `KILL_SWITCH_ACTIVE` defaults to `true` and blocks all order execution while active.
- High-impact news protection is mandatory in Release 2.
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

## Release 2 Guard

Order execution only reaches the paper broker when all of these are true:

- `TRADING_MODE` resolves to `paper`
- `LIVE_TRADING_ENABLED` is `false`
- `KILL_SWITCH_ACTIVE` is `false`
- `trading.require_manual_approval` is `false`
- Risk manager approves the trade
- Strategy confidence is at or above the configured threshold

The default config is deliberately conservative: paper mode, manual approval required, mandatory high-impact news protection, and the kill switch active.
