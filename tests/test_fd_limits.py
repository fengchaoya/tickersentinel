"""常驻运行的资源与取数失败（2026-10-31：Too many open files、内存缓慢上涨、限流被当成休市）。

- yfinance 每次下载（单个代码也一样）新开线程：线程各开一个时区缓存库连接（只有完整垃圾回收才关），
  multitasking 还把每个线程记在 config["TASKS"] 里从不删除 → 每次下载后清理（release_download_threads）；
- yfinance 对每个代码分别记错误：限流、网络出错要和“休市没数据”分开（Bars.failed），采集据此不推进、计入失败；
- 启动时调高本进程的句柄上限。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from datetime import datetime, timedelta

import pandas as pd
import pytest

from tickersentinel.sources import prices_yfinance as py
from tickersentinel.sources.base import NoDataError, SourceError
from tickersentinel.timeutil import UTC

yf = pytest.importorskip("yfinance")
from yfinance import multi as yf_multi  # noqa: E402

START = datetime(2026, 10, 28, 14, 0, tzinfo=UTC)
RATE = "YFRateLimitError('Too Many Requests. Rate limited. Try after a while.')"
CLOSED = "possibly delisted; no price data found  (1m 2026-10-31 14:00:00+00:00 -> 2026-10-31 15:00:00+00:00)"


def yf_frame(symbols: list[str]) -> pd.DataFrame:
    """yf.download(group_by="ticker") 的样子：(Ticker, Price) 两层列。"""
    idx = pd.date_range(START, periods=3, freq="1min")
    cols = pd.MultiIndex.from_product([symbols, ["Open", "High", "Low", "Close", "Volume"]], names=["Ticker", "Price"])
    return pd.DataFrame(1.0, index=idx, columns=cols)


def fake_impl(ok: list[str], errors: dict[str, str], calls: list | None = None):
    """替身：yfinance.multi._download_impl —— 返回 ok 里代码的数据，把 errors 记进 ctx（yfinance 内部转成大写）。"""

    def impl(ctx, tickers, **kw):
        if calls is not None:
            calls.append(list(tickers))
        ctx.errors.update({k.upper(): v for k, v in errors.items()})
        return yf_frame(ok) if ok else pd.DataFrame()

    return impl


def test_every_download_releases_threads_even_single_symbol(monkeypatch):
    calls = []
    monkeypatch.setattr(py, "release_download_threads", lambda: calls.append(1))
    monkeypatch.setattr(yf_multi, "_download_impl", fake_impl(["MU", "SNDK"], {}))
    py.YFinancePriceSource()._download(["MU", "SNDK"], period="1d")
    py.YFinancePriceSource()._download(["MU"], period="1d")  # 单个代码也开线程（threads=True 时 yfinance 照样新开一个）
    assert calls == [1, 1]


def test_release_runs_even_when_download_fails(monkeypatch):
    calls = []
    monkeypatch.setattr(py, "release_download_threads", lambda: calls.append(1))

    def boom(ctx, tickers, **kw):
        raise RuntimeError("x")

    monkeypatch.setattr(yf_multi, "_download_impl", boom)
    with pytest.raises(SourceError):
        py.YFinancePriceSource()._download(["MU", "SNDK"], period="1d")
    assert calls == [1]


def test_release_prunes_finished_multitasking_threads():
    import multitasking

    gate = threading.Event()

    @multitasking.task
    def quick():
        return None

    @multitasking.task
    def waiting():
        gate.wait(5)

    before = list(multitasking.config["TASKS"])
    done = [quick() for _ in range(5)]
    for t in done:
        t.join(5)
    running = waiting()
    try:
        py.release_download_threads()
        tasks = multitasking.config["TASKS"]
        assert not any(t in tasks for t in done)  # 跑完的不再留着
        assert running in tasks  # 还在跑的（别的下载正在用）不动
        assert all(t.is_alive() for t in tasks if t not in before)
    finally:
        gate.set()
        running.join(5)


def test_is_no_data_separates_closed_market_from_fetch_errors():
    # 设了 hide_exceptions = False 后 yfinance 记的是异常的 repr
    assert py.is_no_data("YFPricesMissingError('$MU: possibly delisted; no price data found  (1m 2026-10-31 -> 2026-11-01)')")
    assert py.is_no_data("YFTzMissingError('$XYZ: possibly delisted; no timezone found')")  # 代码不存在：配置问题
    assert py.is_no_data("YFPricesMissingError('$SIVB: No data found, symbol may be delisted')")  # Yahoo 404 给的原因
    # 没设成功时记的是纯文字
    assert py.is_no_data(CLOSED)
    assert py.is_no_data("1m data not available for startTime=1 and endTime=2. The requested range must be within the last 30 days.")
    assert not py.is_no_data(RATE)
    assert not py.is_no_data("Timeout('Failed to perform, curl: (28) Operation timed out after 10001 milliseconds')")
    assert not py.is_no_data("ConnectionError('Failed to perform, curl: (6) Could not resolve host: query2.finance.yahoo.com')")
    assert not py.is_no_data("YFDataException('*** YAHOO! FINANCE IS CURRENTLY DOWN! ***')")


def test_failed_symbol_stops_at_the_break_instead_of_leaving_a_hole(monkeypatch):
    """跨多段（> 7 天）的下载里某个代码中间一段失败：不再取它更新的段，返回的数据停在断掉的地方（下一轮从那里接着取）。"""
    src = py.YFinancePriceSource()
    calls: list[list[str]] = []

    def fake(symbols, **kw):
        calls.append(list(symbols))
        bad = {"MU": RATE} if len(calls) == 2 and "MU" in symbols else {}  # 第二段 MU 被限流
        return yf_frame([s for s in symbols if s not in bad]), bad

    monkeypatch.setattr(src, "_download", fake)
    out = src._intraday_sync(["MU", "SNDK"], START, START + timedelta(days=20), True)
    assert calls[0] == ["MU", "SNDK"] and calls[1] == ["MU", "SNDK"] and all(c == ["SNDK"] for c in calls[2:])
    assert out.failed == {"MU": RATE} and set(out) == {"MU", "SNDK"}  # MU 只有第一段


class FakeResp:
    def __init__(self, status: int, body: dict | None, text: str | None = None):
        import json

        self.status_code = status
        self.text = text if text is not None else json.dumps(body)
        self.url = "https://query2.finance.yahoo.com/v8/finance/chart/MU"
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


@pytest.mark.parametrize("case, failed", [("timeout", True), ("dns", True), ("down", True), ("server_error", True),
                                          ("closed", False), ("delisted", False)])
def test_network_errors_go_through_yfinance_as_failures(monkeypatch, case, failed):
    """走 yfinance 真实的请求路径（只把发请求那一步换掉）：断网、DNS、Yahoo 宕机要算失败，休市、退市算没数据。
    yfinance 默认（hide_exceptions = True）会把前三种吞掉、报成和休市一样的 “no price data found”。"""
    from curl_cffi.requests.exceptions import ConnectionError as CurlConnErr, Timeout as CurlTimeout
    from yfinance import base as yf_base, data as yf_data

    yf.config.debug.hide_exceptions = True  # 从默认值开始：_download 自己会改
    monkeypatch.setattr(yf_base.TickerBase, "_get_ticker_tz", lambda self, timeout: "America/New_York")  # 不碰本机的时区缓存

    def request(self, url, request_method, **kw):
        if case == "timeout":
            raise CurlTimeout("Failed to perform, curl: (28) Operation timed out after 10001 milliseconds")
        if case == "dns":
            raise CurlConnErr("Failed to perform, curl: (6) Could not resolve host: query2.finance.yahoo.com")
        if case == "down":  # Yahoo 宕机时的 HTML 页
            return FakeResp(503, None, text="<html><body>Will be right back...</body></html>")
        if case == "server_error":  # 服务器出错但回了 JSON：yfinance 包成 YFPricesMissingError('$MU: Internal Server Error')
            return FakeResp(500, {"chart": {"result": None, "error": {"code": "Internal Server Error",
                                                                      "description": "Internal Server Error"}}})
        if case == "delisted":
            return FakeResp(404, {"chart": {"result": None, "error": {"code": "Not Found",
                                                                      "description": "No data found, symbol may be delisted"}}})
        return FakeResp(200, {"chart": {"result": [{"meta": {"validRanges": ["1d"]}, "indicators": {"quote": [{}]}}],
                                        "error": None}})  # 休市：有 meta，没有 K 线

    monkeypatch.setattr(yf_data.YfData, "_make_request", request)
    monkeypatch.setattr(py, "release_download_threads", lambda: None)
    src = py.YFinancePriceSource()
    # 过去的时间段 yfinance 走 cache_get（按网址缓存 64 条）：每个用例用不同的时间段，免得读到上一个用例的响应
    t0 = START + timedelta(hours=["timeout", "dns", "down", "server_error", "closed", "delisted"].index(case))
    try:
        _, bad = src._download(["MU"], start=t0, end=t0 + timedelta(minutes=40), interval="1m", prepost=True)
        assert bool(bad) == failed, bad
        with pytest.raises(SourceError) as err:
            src._intraday_sync(["MU"], t0 + timedelta(minutes=1), t0 + timedelta(minutes=41), True)
        assert isinstance(err.value, NoDataError) != failed  # 失败：SourceError；没数据：NoDataError（休市）
    finally:
        yf.config.debug.hide_exceptions = True


def test_intraday_reports_rate_limited_symbols_but_not_closed_ones(monkeypatch):
    monkeypatch.setattr(py, "release_download_threads", lambda: None)
    monkeypatch.setattr(yf_multi, "_download_impl", fake_impl(["MU"], {"SNDK": RATE, "GC=F": CLOSED}))
    out = py.YFinancePriceSource()._intraday_sync(["MU", "SNDK", "GC=F"], START, START + timedelta(hours=1), True)
    assert isinstance(out, py.Bars) and set(out) == {"MU"}
    assert out.failed == {"SNDK": RATE}  # GC=F 只是没数据（休市），不算失败


def test_intraday_all_failed_is_a_source_error_not_market_closed(monkeypatch):
    monkeypatch.setattr(py, "release_download_threads", lambda: None)
    src = py.YFinancePriceSource()
    monkeypatch.setattr(yf_multi, "_download_impl", fake_impl([], {"MU": RATE, "SNDK": CLOSED}))
    with pytest.raises(SourceError) as err:
        src._intraday_sync(["MU", "SNDK"], START, START + timedelta(hours=1), True)
    assert not isinstance(err.value, NoDataError) and "Too Many Requests" in str(err.value)
    monkeypatch.setattr(yf_multi, "_download_impl", fake_impl([], {"MU": CLOSED, "SNDK": CLOSED}))
    with pytest.raises(NoDataError):  # 全部只是没数据：休市
        src._intraday_sync(["MU", "SNDK"], START, START + timedelta(hours=1), True)


def test_falls_back_to_public_download_when_internals_change(monkeypatch):
    monkeypatch.setattr(py, "release_download_threads", lambda: None)
    monkeypatch.delattr(yf_multi, "_download_impl")
    seen = []
    monkeypatch.setattr(yf, "download", lambda tickers, **kw: seen.append(tickers) or yf_frame(["MU"]))
    data, failed = py.YFinancePriceSource()._download(["MU"], period="1d")
    assert seen == [["MU"]] and failed == {} and not data.empty


@pytest.mark.skipif(sys.platform == "win32", reason="没有 resource 模块")
def test_raise_open_file_limit_lifts_low_soft_limit():
    code = textwrap.dedent("""
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
        from tickersentinel.cli import raise_open_file_limit
        raise_open_file_limit(1024)
        print(resource.getrlimit(resource.RLIMIT_NOFILE)[0])
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert int(out.stdout.strip()) >= 1024
