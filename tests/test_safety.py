from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.agent import TradingAgent
from src.broker import AngelOneBroker, PaperBroker
from src.config import load_config
from src.news import NewsSignal
from src.risk import RiskDecision
from src.safety import LiveTradingDisabledError, SafetyConfigError
from src.strategy import TradeSignal


BASE_CONFIG = """
trading:
  mode: paper
  require_manual_approval: false
  confidence_threshold: 75
  symbols:
    - exchange: NSE
      symbol: SBIN-EQ
      token: "3045"
      quantity: 1
      timeframe: FIVE_MINUTE

risk:
  capital: 100000
  risk_per_trade_pct: 0.5
  max_daily_loss_pct: 2.0
  max_trades_per_day: 5
  stop_after_consecutive_losses: 3
  max_position_value_pct: 10
  avoid_high_impact_news: true

strategy:
  ema_fast: 9
  ema_slow: 21
  ema_trend: 50
  rsi_period: 14
  atr_period: 14
  min_reward_risk: 1.5

news:
  enabled: true
  manual_headlines: []

logging:
  decision_log: decisions.jsonl
"""


def write_config(tmp_path: Path, text: str = BASE_CONFIG) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def load_test_config(tmp_path: Path):
    return load_config(write_config(tmp_path))


def signal(confidence: int = 90, decision: str = "BUY") -> TradeSignal:
    return TradeSignal(decision, confidence, "Bullish", 100.0, 99.0, 102.0, 2.0, "test")


class FixedStrategy:
    def __init__(self, trade_signal: TradeSignal) -> None:
        self.trade_signal = trade_signal

    def evaluate(self, candles, news):
        return self.trade_signal


class FixedNews:
    def __init__(self, news_signal: NewsSignal) -> None:
        self.news_signal = news_signal

    def analyze(self, headlines):
        return self.news_signal


class FixedRisk:
    def __init__(self, decision: RiskDecision) -> None:
        self.decision = decision

    def evaluate(self, trade_signal, requested_quantity, news):
        return self.decision


@dataclass
class RecordingBroker:
    calls: int = 0

    def place_order(self, symbol_config, trade_signal, quantity):
        self.calls += 1
        return PaperBroker().place_order(symbol_config, trade_signal, quantity)


def run_agent_with(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trade_signal: TradeSignal,
    risk_decision: RiskDecision,
    *,
    kill_switch: str = "false",
    news_signal: NewsSignal | None = None,
):
    monkeypatch.setenv("KILL_SWITCH_ACTIVE", kill_switch)
    config = load_test_config(tmp_path)
    agent = TradingAgent(config)
    broker = RecordingBroker()
    agent.broker = broker
    agent.strategy = FixedStrategy(trade_signal)
    agent.news_analyzer = FixedNews(news_signal or NewsSignal(0, "Low", [], "test"))
    agent.risk = FixedRisk(risk_decision)

    agent.run_once_with_candles(candles=None)
    return broker


def test_default_paper_mode_and_safety_flags(tmp_path, monkeypatch):
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)
    monkeypatch.delenv("KILL_SWITCH_ACTIVE", raising=False)

    config = load_test_config(tmp_path)

    assert config.safety.trading_mode == "paper"
    assert config.safety.live_trading_enabled is False
    assert config.safety.kill_switch_active is True


def test_live_trading_enabled_true_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")

    with pytest.raises(SafetyConfigError):
        load_test_config(tmp_path)


def test_real_order_submission_is_always_blocked():
    broker = AngelOneBroker({})

    with pytest.raises(LiveTradingDisabledError):
        broker.place_order({"symbol": "SBIN-EQ", "token": "3045"}, signal(), 1)


def test_low_confidence_blocks_execution(tmp_path, monkeypatch):
    broker = run_agent_with(
        tmp_path,
        monkeypatch,
        signal(confidence=74),
        RiskDecision(True, 1, "Risk checks passed"),
    )

    assert broker.calls == 0


def test_high_impact_news_blocks_execution():
    from src.risk import RiskManager

    decision = RiskManager({"avoid_high_impact_news": True}).evaluate(
        signal(),
        1,
        NewsSignal(-40, "High", ["RBI rate hike"], "high impact"),
    )

    assert decision.approved is False
    assert decision.quantity == 0


def test_failed_risk_checks_block_execution(tmp_path, monkeypatch):
    broker = run_agent_with(
        tmp_path,
        monkeypatch,
        signal(),
        RiskDecision(False, 0, "Max daily loss reached"),
    )

    assert broker.calls == 0


def test_manual_approval_setting_cannot_bypass_risk_checks(tmp_path, monkeypatch):
    broker = run_agent_with(
        tmp_path,
        monkeypatch,
        signal(confidence=100),
        RiskDecision(False, 0, "High-impact news risk"),
    )

    assert broker.calls == 0


def test_kill_switch_blocks_all_order_execution(tmp_path, monkeypatch):
    broker = run_agent_with(
        tmp_path,
        monkeypatch,
        signal(confidence=100),
        RiskDecision(True, 1, "Risk checks passed"),
        kill_switch="true",
    )

    assert broker.calls == 0


def test_missing_and_malformed_safety_config_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("LIVE_TRADING_ENABLED", raising=False)

    missing_risk = BASE_CONFIG.replace("risk:", "missing_risk:")
    with pytest.raises(SafetyConfigError):
        load_config(write_config(tmp_path, missing_risk))

    disabled_news_guard = BASE_CONFIG.replace("avoid_high_impact_news: true", "avoid_high_impact_news: false")
    with pytest.raises(SafetyConfigError):
        load_config(write_config(tmp_path, disabled_news_guard))

    malformed_threshold = BASE_CONFIG.replace("confidence_threshold: 75", 'confidence_threshold: "high"')
    with pytest.raises(SafetyConfigError):
        load_config(write_config(tmp_path, malformed_threshold))


def test_paper_order_identification():
    order = PaperBroker().place_order({"symbol": "SBIN-EQ"}, signal(), 1)

    assert order.accepted is True
    assert order.order_id.startswith("PAPER-")
    assert "Paper order" in order.message


def test_no_committed_credential_fields():
    repo = Path(__file__).resolve().parents[1]
    config_files = [repo / "config.example.yaml", repo / "config.yaml"]
    forbidden = ("api_key", "client_code", "password", "totp_secret", "access_token", "refresh_token")

    for path in config_files:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(term in text for term in forbidden)


def test_release_one_source_has_no_place_order_api_call():
    repo = Path(__file__).resolve().parents[1]
    source = (repo / "src" / "broker.py").read_text(encoding="utf-8")

    assert "placeOrder" not in source
