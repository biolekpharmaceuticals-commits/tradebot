# Angel One Auto Trading Agent MVP

This is a cautious starter framework for an Angel One SmartAPI trading agent. Release 1 is paper-trading-only and cannot submit live Angel One orders.

## What It Does

- Loads strategy, risk, and broker settings from `config.example.yaml`
- Analyzes OHLCV candles with EMA, RSI, VWAP, MACD, ATR, and support/resistance
- Scores economic and geopolitical news sentiment with a transparent keyword model
- Combines technical trend, news sentiment, and risk checks into a trade decision
- Runs in paper mode by default
- Provides a broker adapter interface and an Angel One adapter stub that blocks every order submission in Release 1
- Logs every decision as JSON lines

## Important Safety Notes

- This is not financial advice.
- Use paper trading first.
- Release 1 rejects live trading and keeps `TRADING_MODE=paper` by default.
- `LIVE_TRADING_ENABLED` defaults to `false`; setting it to `true` fails closed in Release 1.
- `KILL_SWITCH_ACTIVE` defaults to `true` and blocks all order execution while active.
- High-impact news protection is mandatory in Release 1.
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

Edit `config.yaml` with your symbols and Angel One credentials.

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

## Release 1 Guard

Order execution only reaches the paper broker when all of these are true:

- `TRADING_MODE` resolves to `paper`
- `LIVE_TRADING_ENABLED` is `false`
- `KILL_SWITCH_ACTIVE` is `false`
- `trading.require_manual_approval` is `false`
- Risk manager approves the trade
- Strategy confidence is at or above the configured threshold

The default config is deliberately conservative: paper mode, manual approval required, mandatory high-impact news protection, and the kill switch active.
