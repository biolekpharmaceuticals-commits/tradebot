from __future__ import annotations

import argparse
from pathlib import Path

from src.agent import TradingAgent
from src.config import load_config
from src.demo_data import demo_candles


def main() -> None:
    parser = argparse.ArgumentParser(description="Angel One trading agent MVP")
    parser.add_argument("--config", default="config.example.yaml", help="Path to config YAML")
    parser.add_argument("--demo", action="store_true", help="Use built-in demo candles")
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    agent = TradingAgent(config)

    if args.demo:
        agent.run_once_with_candles(demo_candles())
        return

    agent.run_once()


if __name__ == "__main__":
    main()
