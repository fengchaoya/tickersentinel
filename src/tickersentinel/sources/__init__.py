"""数据源工厂：根据 settings.yaml 与 .env 组装数据源。缺 key 的源会被跳过并给出提示。"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from tickersentinel.config import Settings
from tickersentinel.sources.base import EventSource, NewsQuery, NewsSource, OptionsSource, PriceSource, SourceError

if TYPE_CHECKING:
    from tickersentinel.sources.filings_sec_edgar import SecEdgarFilings

log = logging.getLogger(__name__)

__all__ = [
    "EventSource", "NewsQuery", "NewsSource", "OptionsSource", "PriceSource", "SourceError",
    "build_price_source", "build_news_sources", "build_event_sources",
]


def build_price_source(settings: Settings) -> PriceSource:
    """美股：主源（provider）+ 兜底（fallback）+ 官方收盘价（official_closes）；海外代码（.KS / .T / 汇率）固定走 yfinance。
    取舍和实测见 sources/prices_routing.py。"""
    from tickersentinel.sources.prices_alpaca import AlpacaPriceSource, RoutedPriceSource
    from tickersentinel.sources.prices_routing import FallbackPriceSource, OfficialCloseSource
    from tickersentinel.sources.prices_yfinance import YFinancePriceSource

    yf = YFinancePriceSource()
    cfg = settings.sources.prices
    timeout = settings.sources.http_timeout_seconds
    key_id, secret = settings.secrets.get("alpaca_api_key_id"), settings.secrets.get("alpaca_api_secret_key")
    has_alpaca = bool(key_id and secret)

    def alpaca(feed: str) -> PriceSource:
        return AlpacaPriceSource(key_id, secret, feed=feed, timeout=timeout)

    if cfg.provider == "alpaca":
        primary: PriceSource = alpaca(cfg.alpaca_feed)  # 缺 key 时这里直接报错，提示去填
    else:
        primary = yf
    sip = alpaca("sip") if has_alpaca and (cfg.official_closes or cfg.fallback == "alpaca") else None
    us = primary
    if cfg.fallback == "alpaca" and cfg.provider != "alpaca":
        if has_alpaca:
            us = FallbackPriceSource(primary, alpaca(cfg.alpaca_feed), history=sip)
        else:
            log.info("没有 Alpaca key：行情不设兜底（sources.prices.fallback）")
    elif cfg.fallback == "yfinance" and cfg.provider != "yfinance":
        us = FallbackPriceSource(primary, yf)
    if cfg.official_closes and sip is not None:
        us = OfficialCloseSource(us, sip)
    return RoutedPriceSource(us=us, foreign=yf)


def build_sec(settings: Settings) -> "SecEdgarFilings | None":
    """SEC 客户端只建一个，新闻（公告）和事件（Form 144、6-K）共用，保证限速是全局的。"""
    from tickersentinel.sources.filings_sec_edgar import SecEdgarFilings

    cfg = settings.sources.filings.sec_edgar
    if not cfg.enabled:
        return None
    # 联系邮箱属于个人信息：优先读 .env 里的 SEC_EDGAR_USER_AGENT，不必写进受 git 管理的 settings.yaml
    user_agent = (os.getenv("SEC_EDGAR_USER_AGENT") or "").strip() or cfg.user_agent
    return SecEdgarFilings(user_agent, cfg.forms, settings.sources.http_timeout_seconds)


def build_news_sources(settings: Settings, sec: "SecEdgarFilings | None" = None) -> list[NewsSource]:
    from tickersentinel.sources.news_finnhub import FinnhubSource
    from tickersentinel.sources.news_google_rss import GoogleNewsRSS

    timeout = settings.sources.http_timeout_seconds
    news, filings = settings.sources.news, settings.sources.filings
    out: list[NewsSource] = []

    if news.finnhub.enabled:
        key = settings.secrets.get("finnhub_api_key")
        if key:
            out.append(FinnhubSource(key, timeout))
        else:
            log.warning("未配置 FINNHUB_API_KEY：跳过 Finnhub 新闻")
    if news.google_news_rss.enabled:
        out.append(GoogleNewsRSS(news.google_news_rss.editions, timeout))
    if sec is not None:
        out.append(sec)
    if filings.dart.enabled:
        log.warning("韩国 DART 公告尚未实现，已跳过（见 CLAUDE.md 路线图）")
    return out


def build_event_sources(settings: Settings, sec: "SecEdgarFilings | None" = None) -> list[EventSource]:
    """事件来源（财报日、内部人拟减持、宏观日程……）。缺 key 或未启用的来源跳过。"""
    from tickersentinel.sources.events_earnings import JPXEarningsCalendar, YFinanceEarnings
    from tickersentinel.sources.events_sec import Earnings6KEvents, Form144Events
    from tickersentinel.sources.news_finnhub import FinnhubSource

    ev = settings.events
    if not ev.enabled:
        return []
    names = settings.universe.event_names(ev.related)
    timeout = settings.sources.http_timeout_seconds
    out: list[EventSource] = []
    if ev.sources.yfinance:
        out.append(YFinanceEarnings(names))
    if ev.sources.jpx:
        out.append(JPXEarningsCalendar(names, timeout))
    if sec is not None and ev.sources.sec_6k:
        out.append(Earnings6KEvents(sec, names))
    if sec is not None and ev.sources.sec_144:
        form144 = Form144Events(sec, names, min_value_usd=ev.insider_sale_min_usd)
        form144.scope = set(settings.universe.equity_symbols)  # 内部人减持只关心持仓，不看相关公司
        out.append(form144)
    key = settings.secrets.get("finnhub_api_key")
    if key:
        out.append(FinnhubSource(key, timeout, names=names))
    if ev.macro.enabled:  # 宏观日程：美联储、美国经济数据、国债拍卖、日本央行、韩国出口、期权到期与休市
        from tickersentinel.sources.events_macro import build_macro_event_sources

        out += build_macro_event_sources(settings)
    return out
