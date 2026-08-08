from __future__ import annotations

import traceback
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.agent import TradingAgent
from src.broker import AngelOneBroker, PaperBroker
from src.config import load_config
from src.market_data import (
    AngelOneMarketDataProvider,
    DemoMarketDataProvider,
    MarketDataError,
    build_market_data_provider,
)
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

market_data:
  provider: demo
  lookback_days: 5

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

    unknown_provider = BASE_CONFIG.replace("provider: demo", "provider: unknown")
    with pytest.raises(SafetyConfigError):
        load_config(write_config(tmp_path, unknown_provider))


def test_paper_order_identification():
    order = PaperBroker().place_order({"symbol": "SBIN-EQ"}, signal(), 1)

    assert order.accepted is True
    assert order.order_id.startswith("PAPER-")
    assert "Paper order" in order.message


def test_no_committed_credential_fields():
    repo = Path(__file__).resolve().parents[1]
    config_files = [repo / "config.example.yaml"]
    local_config = repo / "config.yaml"
    if local_config.exists():
        config_files.append(local_config)
    forbidden = ("api_key", "client_code", "password", "totp_secret", "access_token", "refresh_token")

    for path in config_files:
        text = path.read_text(encoding="utf-8").lower()
        assert not any(term in text for term in forbidden)


def test_release_one_source_has_no_place_order_api_call():
    repo = Path(__file__).resolve().parents[1]
    source = "\n".join(path.read_text(encoding="utf-8") for path in (repo / "src").glob("*.py"))

    forbidden_calls = (
        "placeOrder",
        "placeOrderFullResponse",
        "modifyOrder",
        "cancelOrder",
        "gttCreateRule",
        "gttModifyRule",
        "gttCancelRule",
    )
    for call in forbidden_calls:
        assert call not in source


class FakeSmartClient:
    def __init__(self, *, auth_response=None, candle_response=None) -> None:
        self.auth_response = auth_response if auth_response is not None else {"status": True}
        self.candle_response = candle_response if candle_response is not None else {
            "status": True,
            "data": [["2026-08-08T09:15:00+05:30", 100, 105, 99, 102, 1000]],
        }
        self.sessions = []
        self.candle_requests = []
        self.order_api_called = False

    def generateSession(self, client_code, pin, totp_value):
        self.sessions.append((client_code, pin, totp_value))
        return self.auth_response

    def getCandleData(self, params):
        self.candle_requests.append(params)
        return self.candle_response

    def __getattr__(self, name):
        if name in {
            "placeOrder",
            "placeOrderFullResponse",
            "modifyOrder",
            "cancelOrder",
            "gttCreateRule",
            "gttModifyRule",
            "gttCancelRule",
        }:
            self.order_api_called = True
            raise AssertionError(f"Forbidden order API called: {name}")
        raise AttributeError(name)


def set_angel_env(monkeypatch):
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "test_api_key")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "test_client")
    monkeypatch.setenv("ANGEL_ONE_PIN", "1234")
    monkeypatch.setenv("ANGEL_ONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")


def set_sentinel_angel_env(monkeypatch):
    monkeypatch.setenv("ANGEL_ONE_API_KEY", "SENTINEL_API_KEY_SECRET")
    monkeypatch.setenv("ANGEL_ONE_CLIENT_CODE", "SENTINEL_CLIENT_CODE_SECRET")
    monkeypatch.setenv("ANGEL_ONE_PIN", "SENTINEL_PIN_SECRET")
    monkeypatch.setenv("ANGEL_ONE_TOTP_SECRET", "JBSWY3DPEHPK3PXP")


def fixed_clock():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime(2026, 8, 8, 15, 30, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_angel_market_data_missing_environment_variables_fail_closed(monkeypatch):
    for name in AngelOneMarketDataProvider.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)

    provider = AngelOneMarketDataProvider(client_factory=lambda api_key: FakeSmartClient(), clock=fixed_clock)

    with pytest.raises(MarketDataError, match="Missing required Angel One market-data environment variables"):
        provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})


def test_angel_market_data_successful_candle_conversion(monkeypatch):
    set_angel_env(monkeypatch)
    fake_client = FakeSmartClient()
    provider = AngelOneMarketDataProvider(
        lookback_days=2,
        client_factory=lambda api_key: fake_client,
        clock=fixed_clock,
    )

    candles = provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert list(candles.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(candles) == 1
    assert candles.iloc[0]["open"] == 100.0
    assert candles.iloc[0]["volume"] == 1000
    assert fake_client.candle_requests[0]["exchange"] == "NSE"
    assert fake_client.candle_requests[0]["symboltoken"] == "3045"
    assert fake_client.candle_requests[0]["interval"] == "FIVE_MINUTE"
    assert fake_client.candle_requests[0]["fromdate"] == "2026-08-06 15:30"
    assert fake_client.candle_requests[0]["todate"] == "2026-08-08 15:30"


def test_angel_market_data_malformed_responses_fail_closed(monkeypatch):
    set_angel_env(monkeypatch)

    malformed_responses = [
        {"status": False, "data": []},
        {"status": True, "data": []},
        {"status": True, "data": [["2026-08-08T09:15:00+05:30", 100]]},
        {"status": True, "data": [["2026-08-08T09:15:00+05:30", "bad", 105, 99, 102, 1000]]},
    ]

    for response in malformed_responses:
        provider = AngelOneMarketDataProvider(
            client_factory=lambda api_key, response=response: FakeSmartClient(candle_response=response),
            clock=fixed_clock,
        )
        with pytest.raises(MarketDataError):
            provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})


def test_angel_market_data_authentication_failure_is_sanitized(monkeypatch):
    set_angel_env(monkeypatch)
    provider = AngelOneMarketDataProvider(
        client_factory=lambda api_key: FakeSmartClient(auth_response={"status": False, "data": {"jwtToken": "secret"}}),
        clock=fixed_clock,
    )

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert str(exc_info.value) == "Angel One authentication failed"
    assert "secret" not in str(exc_info.value).lower()
    assert "jwt" not in str(exc_info.value).lower()


def assert_sanitized_traceback(exc_info, expected_message: str, sentinels: tuple[str, ...]):
    formatted = "".join(traceback.format_exception(exc_info.type, exc_info.value, exc_info.tb))

    assert str(exc_info.value) == expected_message
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True
    for sentinel in sentinels:
        assert sentinel not in formatted


def test_client_factory_failure_traceback_is_sanitized(monkeypatch):
    set_sentinel_angel_env(monkeypatch)
    sentinels = (
        "SENTINEL_API_KEY_SECRET",
        "SENTINEL_PIN_SECRET",
        "SENTINEL_JWT_TOKEN_SECRET",
        "SENTINEL_REFRESH_TOKEN_SECRET",
        "SENTINEL_FEED_TOKEN_SECRET",
    )
    factory_calls = []

    def failing_factory(api_key):
        factory_calls.append(api_key)
        raise RuntimeError(
            f"{api_key} SENTINEL_PIN_SECRET SENTINEL_JWT_TOKEN_SECRET "
            "SENTINEL_REFRESH_TOKEN_SECRET SENTINEL_FEED_TOKEN_SECRET"
        )

    provider = AngelOneMarketDataProvider(client_factory=failing_factory, clock=fixed_clock)

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert factory_calls == ["SENTINEL_API_KEY_SECRET"]
    assert_sanitized_traceback(exc_info, "Angel One authentication failed", sentinels)


def test_authentication_exception_traceback_is_sanitized(monkeypatch):
    set_sentinel_angel_env(monkeypatch)
    sentinels = (
        "SENTINEL_API_KEY_SECRET",
        "SENTINEL_CLIENT_CODE_SECRET",
        "SENTINEL_PIN_SECRET",
        "SENTINEL_JWT_TOKEN_SECRET",
        "SENTINEL_REFRESH_TOKEN_SECRET",
        "SENTINEL_FEED_TOKEN_SECRET",
    )

    class FailingAuthClient(FakeSmartClient):
        def __init__(self, api_key):
            super().__init__()
            self.api_key = api_key
            self.generate_session_called = False

        def generateSession(self, client_code, pin, totp_value):
            self.generate_session_called = True
            raise RuntimeError(
                f"{self.api_key} {client_code} {pin} SENTINEL_JWT_TOKEN_SECRET "
                "SENTINEL_REFRESH_TOKEN_SECRET SENTINEL_FEED_TOKEN_SECRET"
            )

    fake_client = FailingAuthClient("SENTINEL_API_KEY_SECRET")
    provider = AngelOneMarketDataProvider(client_factory=lambda api_key: fake_client, clock=fixed_clock)

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert fake_client.generate_session_called is True
    assert_sanitized_traceback(exc_info, "Angel One authentication failed", sentinels)


def test_candle_retrieval_exception_traceback_is_sanitized(monkeypatch):
    set_angel_env(monkeypatch)
    sentinels = (
        "SENTINEL_API_KEY_SECRET",
        "SENTINEL_PIN_SECRET",
        "SENTINEL_CANDLE_JWT_TOKEN_SECRET",
        "SENTINEL_CANDLE_REFRESH_TOKEN_SECRET",
        "SENTINEL_CANDLE_FEED_TOKEN_SECRET",
    )

    class FailingCandleClient(FakeSmartClient):
        def __init__(self):
            super().__init__()
            self.get_candle_data_called = False

        def getCandleData(self, params):
            self.get_candle_data_called = True
            raise RuntimeError(
                "SENTINEL_API_KEY_SECRET SENTINEL_PIN_SECRET SENTINEL_CANDLE_JWT_TOKEN_SECRET "
                "SENTINEL_CANDLE_REFRESH_TOKEN_SECRET "
                "SENTINEL_CANDLE_FEED_TOKEN_SECRET"
            )

    fake_client = FailingCandleClient()
    provider = AngelOneMarketDataProvider(client_factory=lambda api_key: fake_client, clock=fixed_clock)

    with pytest.raises(MarketDataError) as exc_info:
        provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert fake_client.get_candle_data_called is True
    assert_sanitized_traceback(exc_info, "Angel One candle retrieval failed", sentinels)


def test_market_data_provider_selection(tmp_path):
    config = load_config(write_config(tmp_path))

    assert isinstance(build_market_data_provider(config.section("market_data")), DemoMarketDataProvider)

    angel_provider = build_market_data_provider({"provider": "angel_one", "lookback_days": 3})
    assert isinstance(angel_provider, AngelOneMarketDataProvider)


def test_trading_agent_uses_paper_broker_and_market_data_provider(tmp_path):
    config = load_config(write_config(tmp_path))
    agent = TradingAgent(config)

    assert isinstance(agent.broker, PaperBroker)
    assert isinstance(agent.market_data, DemoMarketDataProvider)


def test_angel_market_data_never_calls_order_apis(monkeypatch):
    set_angel_env(monkeypatch)
    fake_client = FakeSmartClient()
    provider = AngelOneMarketDataProvider(
        client_factory=lambda api_key: fake_client,
        clock=fixed_clock,
    )

    provider.get_candles({"exchange": "NSE", "token": "3045", "timeframe": "FIVE_MINUTE"})

    assert fake_client.order_api_called is False
