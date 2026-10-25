from datetime import date, datetime

import pytest
from pydantic import ValidationError

from tickersentinel.config import PriceRules, Settings, is_us_symbol, load_settings
from tickersentinel.timeutil import UTC

from conftest import TEST_CONFIG


def test_repo_settings_load():
    s = load_settings(TEST_CONFIG, load_env=False)
    assert s.universe.symbols == ["MU", "SNDK", "SKHY"]
    assert [t.key for t in s.universe.active_themes] == ["memory"] and s.universe.benchmark_for("MU") == "SOXX"
    assert s.universe.intraday_symbols == ["MU", "SNDK", "SKHY", "SOXX", "WDC"]
    assert "000660.KS" in s.universe.foreign_refs and "KRW=X" in s.universe.foreign_refs
    skhy = s.universe.get("SKHY")
    assert skhy.adr_ratio == 10 and skhy.underlying == "000660.KS"
    assert s.notify.provider in ("console", "telegram", "feishu")
    assert set(s.monitor.price.min_move_pct) == set(s.monitor.price.windows_minutes)


def test_typo_in_config_is_rejected():
    with pytest.raises(ValidationError):
        PriceRules(windows_minutes=[5], min_move_pct={5: 1.5}, cooldown_minute=10)  # 少了个 s


def test_missing_window_threshold_is_rejected():
    with pytest.raises(ValidationError):
        PriceRules(windows_minutes=[5, 15], min_move_pct={5: 1.5})


def test_bad_time_format_is_rejected():
    data = {"universe": {"watchlist": [{"symbol": "MU", "name": "x"}]}, "briefs": {"premarket": {"time": "7:30"}}}
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_secrets_are_masked(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value")
    s = load_settings(TEST_CONFIG, load_env=False)
    assert s.secrets.get("anthropic_api_key") == "sk-secret-value"
    assert "sk-secret-value" not in repr(s.secrets)
    assert "sk-secret-value" not in s.model_dump_json()


def test_is_us_symbol():
    assert is_us_symbol("MU") and is_us_symbol("SKHY")
    assert not is_us_symbol("000660.KS") and not is_us_symbol("285A.T") and not is_us_symbol("KRW=X")


def test_calendar_sessions(calendar):
    assert calendar.session(date(2026, 10, 12)) is not None  # 哥伦布日：美股照常交易
    assert calendar.session(date(2026, 11, 26)) is None  # 感恩节休市
    early = calendar.session(date(2026, 11, 27))
    assert early.early_close and early.close == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)  # 13:00 ET
    normal = calendar.session(date(2026, 10, 9))
    assert normal.minutes == 390 and normal.open == datetime(2026, 10, 9, 13, 30, tzinfo=UTC)
    # 11 月 1 日夏令时结束后，开盘 UTC 时间变为 14:30
    assert calendar.session(date(2026, 11, 2)).open == datetime(2026, 11, 2, 14, 30, tzinfo=UTC)


def test_calendar_helpers(calendar):
    prior = calendar.sessions_before(date(2026, 10, 12), 3)
    assert [s.day for s in prior] == [date(2026, 10, 7), date(2026, 10, 8), date(2026, 10, 9)]
    assert calendar.previous_session(date(2026, 10, 11)).day == date(2026, 10, 9)  # 周日 → 周五
    s = calendar.session(date(2026, 10, 9))
    assert calendar.session_at(datetime(2026, 10, 9, 15, 0, tzinfo=UTC)) == s
    assert calendar.session_at(datetime(2026, 10, 9, 20, 0, tzinfo=UTC)) is None  # 收盘那一刻不算盘中
    done = calendar.last_completed_sessions(datetime(2026, 10, 9, 21, 0, tzinfo=UTC), 2)
    assert [x.day for x in done] == [date(2026, 10, 8), date(2026, 10, 9)]
    during = calendar.last_completed_sessions(datetime(2026, 10, 9, 15, 0, tzinfo=UTC), 2)
    assert [x.day for x in during] == [date(2026, 10, 7), date(2026, 10, 8)]
