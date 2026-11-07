"""分钟线采集（决策 21）：tsen run 每隔 bars.collect_seconds（默认 2 分钟）把分钟线补进本地库（barstore.py）。

采哪些代码（targets）：
- 宏观变量：settings.yaml 的 macro.indicators 和生效主题的 indicators（symbol 与 off_hours_symbol 都采，盘前盘后都要）；
- 监控分组（jobs/monitor.build_groups）里每组要取的代码：持仓、基准、同业、夜间信号、篮子成分；hours: 24x5 的持仓另取夜盘。
监控范围热更新（App.apply_scope）时 reload()，新加的代码自动补历史。

怎么取：
- 白天（含盘前盘后）一律用 yfinance，不走行情路由的 Alpaca 兜底：同一个代码的分钟线不混用两个来源（决策 18：成交量口径不同）；
- 夜盘用行情源的 overnight_bars（Hyperliquid，只返回夜盘时段），来源记为 hyperliquid。

增量：每个代码从“库里最后一根和上次查到的时刻里较晚的那个”往前 OVERLAP（40 分钟）取到现在——期货、利率在 Yahoo 上延迟 10–15 分钟，
首尔、东京延迟 20 分钟，最近几根还会被修正，同一分钟再写一次就覆盖。起点相近（RECENT 以内）的合成一次批量下载；停机后的空档单独一批补，
最多补 backfill_days。从没采过的代码在后台逐个补 backfill_days（默认 29 天：Yahoo 的 1 分钟线只保留约 30 天），不耽误这一轮的增量。

取数失败和休市要分清（2026-10-31）：yfinance 对每个代码分别记错误，“没数据”（休市、没成交）照常推进“查到哪一刻”；
限流、网络出错的代码不推进，下一轮从库里最后一根往前重取，恢复后自动补上断掉的那段（以前一律当休市，那段就永久缺了）。

顺带：财政部收益率曲线每 12 小时刷新一次（全年逐日，画日线）；超过 keep_minute_days 的 1 分钟线每天压一次成 5 分钟线。
失败记日志和状态（App 写进心跳，网页顶栏小标悬停能看到）；连续失败 bars.alert_after_failures 轮推一条提醒（App.collect_bars），
每段连续失败只推一次。分钟线库只服务网页，盯盘自己取数，不受影响。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from tickersentinel.config import is_pseudo
from tickersentinel.sources.base import NoDataError, SourceError
from tickersentinel.timeutil import utcnow

if TYPE_CHECKING:
    from tickersentinel.barstore import BarStore
    from tickersentinel.config import Settings
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources.base import PriceSource
    from tickersentinel.sources.macro import MacroSource

log = logging.getLogger(__name__)

# 往前多取一段：期货、利率在 Yahoo 上延迟 10–15 分钟，首尔、东京标明延迟 20 分钟（exchangeDataDelayedBy），最近几根还会被修正。
# 起点 = 上一轮查到的时刻往前这么多，所以延迟必须小于它：20 分钟时首尔、东京正好卡在边上，晚到一两分钟的 K 线就漏了。
OVERLAP = timedelta(minutes=40)
RECENT = timedelta(hours=2)
RETRY_FAILED = timedelta(minutes=30)  # 补历史失败的代码，隔这么久再试
CURVE_EVERY = timedelta(hours=12)
COMPACT_EVERY = timedelta(hours=24)


@dataclass(frozen=True)
class Want:
    prepost: bool = True
    night: bool = False


@dataclass
class CollectStatus:
    last_run: datetime | None = None
    last_ok: datetime | None = None
    last_error: str | None = None
    failures: int = 0  # 连续失败的轮数
    rows: int = 0  # 上一轮写入的行数
    backfilling: list[str] = field(default_factory=list)

    def to_json(self, symbols: int) -> dict[str, Any]:
        return {"last_run": self.last_run.isoformat() if self.last_run else None,
                "last_ok": self.last_ok.isoformat() if self.last_ok else None,
                "last_error": self.last_error, "failures": self.failures, "rows": self.rows,
                "symbols": symbols, "backfilling": len(self.backfilling)}


def collect_targets(settings: "Settings", calendar: "MarketCalendar") -> dict[str, Want]:
    """要存的代码 → 怎么取（盘前盘后、夜盘）。同一个代码出现在几处时取并集。"""
    from tickersentinel.jobs.monitor import build_groups

    out: dict[str, Want] = {}

    def add(sym: str | None, prepost: bool, night: bool) -> None:
        if not sym or is_pseudo(sym):
            return
        old = out.get(sym)
        out[sym] = Want(prepost or (old.prepost if old else False), night or (old.night if old else False))

    if settings.macro.enabled:
        inds = [*settings.macro.indicators, *(i for th in settings.universe.active_themes for i in th.indicators)]
        for ind in inds:
            add(ind.symbol, True, False)
            add(ind.off_hours_symbol, True, False)
            add(ind.proxy_symbol, True, False)  # 收益率夜里没数据：存对应的国债期货，网页用它折算
    try:
        groups = build_groups(settings, calendar)
    except Exception as exc:  # 配置或交易日历出问题：至少把宏观变量存下来
        log.warning("分钟线采集：监控分组失败，只采宏观变量：%s", exc)
        groups = []
    for g in groups:
        for s in g.fetch:
            add(s, g.prepost, g.night and g.hours.get(s) == "24x5")
    return out


class BarCollector:
    def __init__(self, settings: "Settings", calendar: "MarketCalendar", store: "BarStore", *,
                 day: "PriceSource | None" = None, night: "PriceSource | None" = None,
                 macro: "MacroSource | None" = None, clock=utcnow):
        from tickersentinel.sources.prices_yfinance import YFinancePriceSource

        self.settings = settings
        self.calendar = calendar
        self.store = store
        self.day = day or YFinancePriceSource()
        self.night = night
        self.macro = macro
        self.clock = clock
        self.status = CollectStatus()
        self._targets: dict[str, Want] | None = None
        self._checked: dict[str, datetime] = {}  # 这个进程里最后一次成功查到哪一刻（休市的代码不必每轮从库里最后一根重查）
        self._failed: dict[str, datetime] = {}  # 补历史失败的时刻
        self._backfill_task: asyncio.Task | None = None
        self._curve_at: datetime | None = None
        self._compact_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def cfg(self):
        return self.settings.bars

    def targets(self) -> dict[str, Want]:
        if self._targets is None:
            self._targets = collect_targets(self.settings, self.calendar)
        return self._targets

    def reload(self) -> None:
        """监控范围变了（App.apply_scope）：下一轮重新算要采的代码，新代码自动补历史。"""
        self._targets = None

    def status_json(self) -> dict[str, Any]:
        return self.status.to_json(len(self._targets or {}))

    # ───────── 每一轮 ─────────

    async def collect(self, now: datetime | None = None) -> int:
        """增量补一轮，返回写入的行数。从没采过的代码交给后台补历史（不等它）。"""
        async with self._lock:
            now = now or self.clock()
            self.status.last_run = now
            targets = self.targets()
            new = [s for s in targets if s not in self._checked and self.store.last_ts(s) is None]
            starts = {s: self._start(s, now) for s in targets if s not in new}
            rows, errors = 0, []
            for (prepost, tier), syms in self._batches(starts, targets, now).items():
                start = max(min(starts[s] for s in syms), now - timedelta(days=max(self.cfg.backfill_days, 1)))
                try:
                    n, failed = await self._fetch_day(syms, start, now, prepost=prepost)
                    rows += n
                    if failed:  # 一部分代码取数失败（限流、网络）：其他照样入库，这几个下一轮从断掉的地方再取
                        sample = "、".join(sorted(failed)[:5]) + ("等" if len(failed) > 5 else "")
                        errors.append(f"{len(failed)} 个代码取数失败（{sample}）：{next(iter(failed.values()))[:150]}")
                except SourceError as exc:
                    errors.append(str(exc))
            night = [s for s in starts if targets[s].night]
            if night and self.night is not None:
                start = min(starts[s] for s in night)
                try:
                    rows += await self._fetch_night(night, start, now)
                except SourceError as exc:  # 夜盘取不到只少了夜盘
                    log.warning("分钟线采集：夜盘失败：%s", exc)
            self.status.rows = rows
            if errors:
                self.status.failures += 1
                self.status.last_error = "；".join(errors)[:300]
                log.warning("分钟线采集失败（连续第 %d 轮）：%s", self.status.failures, self.status.last_error)
            else:
                self.status.failures = 0
                self.status.last_ok = now
            # 补历史失败过的（包括只补到一部分、库里已经有数据的）：过了 RETRY_FAILED 整段再补一次，把中间的空档填上
            retry = [s for s in self._failed if s in targets and s not in new]
            if new or retry:
                self._spawn_backfill(new + retry)
        await self._housekeeping(now)
        return rows

    def _start(self, sym: str, now: datetime) -> datetime:
        last = self.store.last_ts(sym)
        checked = self._checked.get(sym)
        ref = max([t for t in (last, checked) if t is not None], default=now)
        return ref - OVERLAP

    @staticmethod
    def _batches(starts: dict[str, datetime], targets: dict[str, Want], now: datetime) -> dict[tuple[bool, str], list[str]]:
        """起点相近的合成一次下载：按（盘前盘后、最近 / 空档）分批。"""
        out: dict[tuple[bool, str], list[str]] = {}
        for s, start in starts.items():
            tier = "recent" if start >= now - RECENT else "gap"
            out.setdefault((targets[s].prepost, tier), []).append(s)
        return out

    async def _fetch_day(self, syms: list[str], start: datetime, end: datetime, *,
                         prepost: bool) -> tuple[int, dict[str, str]]:
        """取一批代码写进库。返回（写入的行数，{取数失败的代码: 原因}）。
        失败的代码（限流、网络；数据源在结果的 failed 里列出）不推进“查到哪一刻”：下一轮还从库里最后一根往前取，
        恢复后把断掉的那段补上。休市没数据的照常推进（不然周末每一轮都从周五重取）。"""
        try:
            bars = await self.day.intraday_bars(syms, start=start, end=end + timedelta(minutes=1), prepost=prepost)
        except NoDataError:  # 休市、周末：这段时间本来就没有 K 线
            bars = {}
        failed = {s: e for s, e in (getattr(bars, "failed", None) or {}).items() if s in syms}
        rows = sum(self.store.upsert(s, df, "yfinance") for s, df in bars.items())
        for s in syms:
            if s not in failed:
                self._checked[s] = end
        return rows, failed

    async def _fetch_night(self, syms: list[str], start: datetime, end: datetime) -> int:
        bars = await self.night.overnight_bars(syms, start=start, end=end)
        return sum(self.store.upsert(s, df, "hyperliquid") for s, df in bars.items())

    # ───────── 补历史 ─────────

    def _spawn_backfill(self, symbols: list[str]) -> None:
        now = self.clock()
        todo = [s for s in symbols if not (s in self._failed and now - self._failed[s] < RETRY_FAILED)]
        if not todo or (self._backfill_task is not None and not self._backfill_task.done()):
            return
        self._backfill_task = asyncio.create_task(self.backfill(todo))

    async def backfill(self, symbols: list[str] | None = None, *, days: int | None = None,
                       now: datetime | None = None) -> dict[str, int]:
        """逐个代码补最近 days 天（默认 bars.backfill_days）。返回每个代码写入的行数（失败的记 -1）。"""
        now = now or self.clock()
        days = self.cfg.backfill_days if days is None else days
        targets = self.targets()
        symbols = list(symbols if symbols is not None else targets)
        start = now - timedelta(days=days)
        out: dict[str, int] = {}
        self.status.backfilling = list(symbols)
        try:
            for sym in symbols:
                want = targets.get(sym, Want())
                try:
                    n, failed = await self._fetch_day([sym], start, now, prepost=want.prepost)
                    if failed:  # 有一段没取到：取到的照样留着，记为失败，过 RETRY_FAILED 整段再补一次
                        raise SourceError(failed[sym])
                    if want.night and self.night is not None:
                        try:
                            n += await self._fetch_night([sym], start, now)
                        except SourceError as exc:
                            log.warning("补夜盘历史失败（%s）：%s", sym, exc)
                    out[sym] = n
                    self._failed.pop(sym, None)
                    log.info("分钟线库：%s 补了 %d 行（最近 %d 天）", sym, n, days)
                except SourceError as exc:
                    out[sym] = -1
                    self._failed[sym] = self.clock()
                    log.warning("补分钟线历史失败（%s）：%s", sym, exc)
                if sym in self.status.backfilling:
                    self.status.backfilling.remove(sym)
        finally:
            self.status.backfilling = []
        return out

    async def wait_backfill(self) -> None:
        if self._backfill_task is not None:
            await asyncio.gather(self._backfill_task, return_exceptions=True)

    # ───────── 收益率曲线、压缩 ─────────

    async def _housekeeping(self, now: datetime) -> None:
        if self.macro is not None and (self._curve_at is None or now - self._curve_at >= CURVE_EVERY):
            try:
                await self.collect_curve(now)
                self._curve_at = now
            except SourceError as exc:
                log.warning("财政部收益率曲线入库失败：%s", exc)
                self._curve_at = now - CURVE_EVERY + timedelta(hours=1)  # 一小时后再试
        if self._compact_at is None or now - self._compact_at >= COMPACT_EVERY:
            self._compact_at = now
            try:
                n = await asyncio.to_thread(self.store.compact, now - timedelta(days=self.cfg.keep_minute_days))
                if n:
                    log.info("分钟线库：%d 行超过 %d 天的 1 分钟线压成了 5 分钟线", n, self.cfg.keep_minute_days)
            except Exception:  # pragma: no cover - 压缩失败不影响采集
                log.exception("分钟线库压缩失败")

    async def collect_curve(self, now: datetime | None = None) -> int:
        """今年的全年逐日曲线；更早的年份库里没有才取（treasury_years 年）。"""
        if self.macro is None:
            return 0
        now = now or self.clock()
        have = self.store.curve(date(now.year - self.cfg.treasury_years + 1, 1, 1))
        have_years = {d.year for d in have.index} if not have.empty else set()
        rows = 0
        for year in range(now.year - self.cfg.treasury_years + 1, now.year + 1):
            if year != now.year and year in have_years:
                continue
            curves = await self.macro.treasury_year(year)
            rows += self.store.upsert_curve((c.day, c.yields) for c in curves)
        return rows

    async def aclose(self) -> None:
        if self._backfill_task is not None and not self._backfill_task.done():
            self._backfill_task.cancel()
            await asyncio.gather(self._backfill_task, return_exceptions=True)
