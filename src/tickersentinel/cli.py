"""命令行入口：tsen <命令>（或 python -m tickersentinel <命令>；tsen 是 tickersentinel 的简写）。

  run                       常驻运行：定时简报 + 全天候异动监控 + 事件日历 + 飞书对话
  brief premarket|close|weekly [--send]
                            立即生成一份简报（默认只打印，加 --send 才走推送渠道）
  tick [--send]             立即检测一次（各资产按自己的交易时段：美股盘前 / 盘中 / 盘后、海外交易所、期货）
  replay [--days 5] [--symbols MU,night:memory:XKRX]
                            用最近 N 个交易日的真实分钟数据回放检测，评估阈值会推送多少条（含合并后的条数）
  events [--send]           刷新事件日历（财报日、内部人拟减持……）并生成一份汇总（默认只打印）
  check                     检查配置、密钥和各数据源的连通性
  macro                     宏观快照（美债利率、原油、金银、美股大盘、VIX、美元……）
  assets                    各资产的警报标准，换算成相当于最近几倍的波动（σ）
  scope                     查看监控范围（持仓 × 主题：基准、同业、夜间信号、相关公司、检索词、板块指标）
  scope add 代码 [--drop A,B] [--keep C]
                            给新持仓找联动、出方案（并入已有主题或新建），回放预览，确认后写入；suggest 只出方案
  scope remove 代码 | undo [--force]
                            移除持仓（主题保留备用）/ 撤销最近一次改动（之后文件又被改过时要 --force）
  scope prompt 代码         打印在 Claude Code 里准备联动候选需要的上下文和格式（结果写到 data/scope_candidates/代码.json）
  scope check               联动体检：同业、夜间信号的联动是否变弱，有没有新的强联动候选
  scope tune 代码 [--day 5,8,12] [--rapid 5:1.5,15:2.5,30:3,60:3]
                            调警报阈值：不给数字就按日波动建议；回放对比改前改后的推送量，确认后写入
  feishu setup | test       飞书：建立长连接并查出 chat_id / 发一条测试消息
  feishu calendar-setup | bitable-setup [--open-id ou_xxx]
                            飞书：一次性建好事件日历 / 异动多维表格并共享给你，打印链接
  feishu sync               飞书：同步事件日历、补齐多维表格、拉回你填的 label / notes
  ask "问题"                在终端里向大模型提问（和在飞书里给机器人发消息是同一套逻辑），测试对话效果
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from datetime import timedelta

from tickersentinel.config import PROJECT_ROOT, Settings, is_pseudo, load_settings

FEISHU_SYNC_ACTIONS = ("calendar-setup", "bitable-setup", "sync")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "httpcore", "peewee", "apscheduler", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # 失败由我们自己的 SourceError 汇总报告


def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="tsen", description="tickersentinel：按持仓定制的盯盘与投研小助理")
    parser.add_argument("--config", help="settings.yaml 路径（默认 config/settings.yaml）")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="常驻运行：定时简报 + 全天候异动监控 + 事件日历 + 飞书对话")
    p = sub.add_parser("brief", help="立即生成一份简报")
    p.add_argument("kind", choices=["premarket", "close", "weekly"])
    p.add_argument("--send", action="store_true", help="通过配置的推送渠道发送，并记录已读新闻")
    p = sub.add_parser("tick", help="立即检测一次（各资产按自己的交易时段）")
    p.add_argument("--send", action="store_true", help="通过配置的推送渠道发送，并写入数据库")
    p = sub.add_parser("replay", help="用最近 N 个交易日的分钟数据回放异动检测")
    p.add_argument("--days", type=int, default=5, help="回放最近几个交易日（默认 5；yfinance 的 1 分钟线只有约 30 天）")
    p.add_argument("--symbols", help="只回放这些代码，逗号分隔，如 MU,SNDK；夜间篮子写伪代码，如 night:memory:XKRX")
    p = sub.add_parser("events", help="刷新事件日历并生成一份汇总")
    p.add_argument("--send", action="store_true", help="通过配置的推送渠道发送，并记为已汇总")
    sub.add_parser("check", help="检查配置、密钥与数据源连通性")
    sub.add_parser("macro", help="查看宏观快照：美债利率、原油、金银、美股大盘、VIX、美元……")
    sub.add_parser("assets", help="列出盯盘资产与警报标准，并和最近的波动对比（调 config/assets.yaml 时参考）")
    p = sub.add_parser("scope", help="监控范围：查看 / 出方案（suggest）/ 加入（add）/ 移除 / 撤销 / 联动体检 / 调阈值（tune）/ "
                                     "给 Claude Code 准备候选（prompt）")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "suggest", "add", "remove", "undo", "check", "tune", "prompt"])
    p.add_argument("symbol", nargs="?", help="代码，如 LITE；海外股票带交易所后缀，如 300308.SZ")
    p.add_argument("--theme", help="并入这个已有主题（不写就自动判断：并入联动强的已有主题，或新建）")
    p.add_argument("--no-llm", action="store_true", help="不调用 llm.tasks.scope 配的大模型（默认本来就不调用；准备好的候选文件照用）")
    p.add_argument("--force", action="store_true", help="undo：那次改动之后文件又被改过时也照样还原（会丢掉之后的改动）")
    p.add_argument("--candidates", help="Claude Code 准备的候选文件（JSON）；不写时找 data/scope_candidates/代码.json")
    p.add_argument("--yes", "-y", action="store_true", help="不再询问，直接写入（不是交互终端时不加这个就只预览、不写入）")
    p.add_argument("--drop", help="add / suggest：从方案里去掉这些候选，逗号分隔，如 GLW,VIAV")
    p.add_argument("--keep", help="add / suggest：把默认不选的候选加回来，逗号分隔")
    p.add_argument("--replay-days", type=int, default=None, help="写入前回放几天（add 默认 5，tune 默认 10；0 = 不回放）")
    p.add_argument("--day", "--levels", dest="day", help="tune：全天累计的档位（%%），如 5,8,12；不写就按日波动建议")
    p.add_argument("--rapid", help="tune：急涨急跌下限，如 5:1.5,15:2.5,30:3,60:3（窗口分钟:从高低点算起的涨跌幅%%）")
    p = sub.add_parser("ask", help="在终端里向大模型提问，测试对话效果")
    p.add_argument("question", help="问题（带空格时用引号括起来）")
    p.add_argument("--keep", action="store_true", help="记住这次问答（与终端会话共享记忆，发 /new 清空）")
    p = sub.add_parser("feishu", help="飞书设置助手")
    p.add_argument("action", choices=["setup", "test", *FEISHU_SYNC_ACTIONS],
                   help="setup：建立长连接，打印发给机器人的消息所在会话的 chat_id；test：向 FEISHU_CHAT_ID 发测试消息；"
                        "calendar-setup / bitable-setup：建好飞书日历 / 多维表格并共享给你；sync：立即同步一次")
    p.add_argument("--open-id", help="你的飞书 open_id（ou_ 开头）。一般不用填：会从和机器人的会话里自动找出来")
    args = parser.parse_args(argv)

    settings = load_settings(args.config)
    setup_logging("DEBUG" if args.verbose else settings.log_level)
    try:
        asyncio.run(dispatch(args, settings))
    except KeyboardInterrupt:
        pass


async def dispatch(args: argparse.Namespace, settings: Settings) -> None:
    from tickersentinel.app import App

    if args.cmd == "run":
        async with App(settings) as app:
            await app.run_forever()
    elif args.cmd == "brief":
        async with App(settings, dry_run=not args.send) as app:
            if not args.send:  # 预览用内存数据库：先刷新事件日历，简报里才有“近期日程”
                await app.refresh_events()
            await app.run_brief(args.kind, force=True)  # 手动要求的简报：不受“每天一份”的限制
    elif args.cmd == "tick":
        await cmd_tick(settings, send=args.send)
    elif args.cmd == "replay":
        await cmd_replay(settings, args.days, args.symbols)
    elif args.cmd == "events":
        await cmd_events(settings, send=args.send)
    elif args.cmd == "check":
        await cmd_check(settings)
    elif args.cmd == "macro":
        await cmd_macro(settings)
    elif args.cmd == "scope":
        await cmd_scope(settings, args)
    elif args.cmd == "assets":
        await cmd_assets(settings)
    elif args.cmd == "feishu" and args.action in FEISHU_SYNC_ACTIONS:
        await cmd_feishu_sync(settings, args.action, open_id=args.open_id)
    elif args.cmd == "feishu":
        await cmd_feishu(settings, args.action)
    elif args.cmd == "ask":
        await cmd_ask(settings, args.question, keep=args.keep)


async def cmd_assets(settings: Settings) -> None:
    from tickersentinel.jobs.calibrate import asset_report
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources import build_price_source

    prices = build_price_source(settings)
    try:
        print(await asset_report(settings, MarketCalendar("XNYS"), prices))
    finally:
        await prices.aclose()


def _confirmed(args) -> bool:
    """写配置前的确认：--yes 直接写；交互终端里问一句；不是交互终端（Claude Code、脚本）就只预览、不写。"""
    if args.yes:
        return True
    if not sys.stdin.isatty():
        print("（只是预览，没有写入：确认后加 --yes 再运行一次）")
        return False
    try:
        return input("确认写入？[y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _split_symbols(text: str | None) -> list[str]:
    return [x for x in re.split(r"[,，\s]+", text or "") if x]


async def cmd_scope(settings: Settings, args) -> None:
    """tsen scope [show | suggest 代码 | add 代码 | remove 代码 | undo | check | tune 代码 | prompt 代码]。"""
    from tickersentinel.scope import describe_scope
    from tickersentinel.scope.discover import ScopeError, propose, render_proposal
    from tickersentinel.scope.edit import (
        ScopeEditError,
        ScopeEditor,
        describe_change_effects,
        replay_preview,
        settings_after,
    )

    action = args.action
    if action == "show":
        print(describe_scope(settings))
        return
    editor = ScopeEditor(settings)
    if action == "undo":
        hist = editor.history()
        if not hist:
            print("没有可以撤销的改动。")
            return
        last = hist[-1]
        print(f"将撤销最近一次改动：{last['summary']}（{last['at'][:16]}）\n"
              + "\n".join(f"- 还原 {p}" if b else f"- 删掉 {p}" for p, b in last["backups"].items()))
        if not _confirmed(args):
            return
        try:
            msg, new = editor.undo(force=args.force)
        except ScopeEditError as exc:
            print(exc)
            return
        print(msg + "\n" + "\n".join(describe_change_effects(settings, new)) + "\n正在运行的 tsen run 一分钟内自动读到。")
        return
    if action == "check":
        from tickersentinel.scope.health import linkage_report
        from tickersentinel.sources import build_price_source

        prices = build_price_source(settings)
        try:
            print(await linkage_report(settings, prices))
        finally:
            await prices.aclose()
        return
    if not args.symbol:
        print(f"用法：tsen scope {action} 代码（例如 tsen scope {action} LITE）")
        return
    if action == "prompt":
        from tickersentinel.scope.discover import candidates_brief

        print(await candidates_brief(settings, args.symbol))
        return
    if action == "tune":
        from tickersentinel.scope.discover import ScopeError as _ScopeError
        from tickersentinel.scope.tune import parse_numbers, propose_tune, render_tune
        from tickersentinel.sources import build_price_source

        day = parse_numbers(args.day)[0] if args.day else None
        rapid = parse_numbers(args.rapid)[1] if args.rapid else None
        prices = build_price_source(settings)
        try:
            days = 10 if args.replay_days is None else args.replay_days
            print(f"正在算 {args.symbol} 的日波动" + (f"，并回放最近 {days} 个交易日对比推送量" if days else "") + "……")
            try:
                plan = await propose_tune(settings, args.symbol, prices, day=day, rapid=rapid, replay_days=days)
            except (_ScopeError, ScopeEditError) as exc:
                print(exc)
                return
            print(render_tune(plan))
            if plan.unchanged or plan.change is None:
                return
            if not _confirmed(args):
                print("没有改动。")
                return
            editor.apply(plan.change)
            print("已写入（tsen scope undo 可以撤销）。正在运行的 tsen run 会在一分钟内自动读到。")
        finally:
            await prices.aclose()
        return
    if action == "remove":
        try:
            change = editor.plan_remove(args.symbol)
        except ScopeEditError as exc:
            print(exc)
            return
        new = settings_after(settings, change)
        print(change.describe())
        print("\n".join(f"- {x}" for x in describe_change_effects(settings, new)))
        if not _confirmed(args):
            print("没有改动。")
            return
        editor.apply(change)
        print("已写入（tsen scope undo 可以撤销）。正在运行的 tsen run 一分钟内自动读到。")
        return

    from tickersentinel.llm.client import LLMClient
    from tickersentinel.sources import build_price_source

    prices = build_price_source(settings)
    llm = None if args.no_llm else LLMClient(settings.llm)
    try:
        print(f"正在给 {args.symbol.upper()} 找联动（Yahoo / Finnhub / 准备好的候选，近一年日线打分），约需 10–30 秒……")
        try:
            prop = await propose(settings, args.symbol, prices, llm=llm, theme=args.theme, candidates_file=args.candidates)
        except ScopeError as exc:
            print(exc)
            return
        for sym in _split_symbols(args.drop):
            if not prop.drop(sym):
                print(f"（方案里没有可以去掉的 {sym.upper()}）")
        for sym in _split_symbols(args.keep):
            if not prop.restore(sym):
                print(f"（方案里没有可以加回的 {sym.upper()}）")
        print(render_proposal(prop, settings))
        if action == "suggest":
            return
        try:
            change = editor.plan_add(prop)
        except ScopeEditError as exc:
            print(exc)
            return
        new = settings_after(settings, change)
        print("\n" + change.describe())
        print("\n".join(f"- {x}" for x in describe_change_effects(settings, new)))
        days = 5 if args.replay_days is None else args.replay_days
        if days > 0:
            watch = [prop.symbol, *(i.symbol for i in new.universe.monitored
                                     if not i.holding and prop.symbol in i.related_to)]
            try:
                print("- " + await replay_preview(new, prices, watch, days))
            except Exception as exc:
                print(f"- 回放没做成（{type(exc).__name__}: {exc}），不影响写入")
        if not _confirmed(args):
            print("没有改动。")
            return
        try:
            editor.apply(change)
        except ScopeEditError as exc:
            print(exc)
            return
        print("已写入（tsen scope undo 可以撤销）。正在运行的 tsen run 一分钟内自动读到。")
    finally:
        await prices.aclose()


async def cmd_macro(settings: Settings) -> None:
    from tickersentinel.agents.render import render_macro
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources.macro import MacroSource

    src = MacroSource(settings, MarketCalendar("XNYS"))
    msg = render_macro(await src.snapshot(use_cache=False), settings)
    print(f"{msg.title}\n{msg.body}")


async def cmd_tick(settings: Settings, send: bool) -> None:
    from tickersentinel.app import App
    from tickersentinel.timeutil import fmt_local, utcnow

    async with App(settings, dry_run=not send) as app:
        now = utcnow()
        live = [g.key for g in app.monitor.groups if (seg := g.clock.segment_at(now)) is not None and g.active(seg)]
        if not live:  # 各资产按自己的钟：美股盘前 / 盘中 / 盘后、海外交易所、期货、加密货币
            nxt = app.calendar.next_session(app.calendar.exchange_date(now))
            if nxt.open <= now:
                nxt = app.calendar.next_session(nxt.day + timedelta(days=1))
            print(f"现在没有资产在交易时段内。美股下一次开盘：{fmt_local(nxt.open, settings.tz, '%m-%d %H:%M')}（本地时间）")
            print("想看检测效果可以先用：tsen replay --days 5")
            return
        alerts = await app.monitor.tick(now)
        await app.monitor.drain()
        print(f"本次检测：{len(alerts)} 条异动" + ("" if alerts else "（没有异动就不推送）"))


async def cmd_events(settings: Settings, send: bool) -> None:
    from tickersentinel.app import App

    async with App(settings, dry_run=not send) as app:
        res = await app.events.refresh()
        print(f"事件来源：{'，'.join(f'{k} {v} 条' for k, v in res.per_source.items()) or '无'}"
              + (f"｜失败：{res.errors}" if res.errors else ""))
        msg = await app.digest.build()
        if msg is None:
            print("没有明天的事件，也没有新增 / 改期：今晚不会推送。")
            return
        if not send:
            print("（预览：用的是全新的内存数据库，内容等同于第一次汇总；实际每晚只推送明天的事件和新增 / 改期）")
        await app.notifier.send(msg)
        if send:
            app.digest.mark_sent()


async def cmd_replay(settings: Settings, days: int, symbols: str | None) -> None:
    from tickersentinel.jobs.replay import run_replay
    from tickersentinel.market_calendar import MarketCalendar
    from tickersentinel.sources import build_price_source

    from tickersentinel.sources.base import SourceError

    prices = build_price_source(settings)
    try:
        syms = ([s.strip() if is_pseudo(s.strip()) else s.strip().upper() for s in symbols.split(",") if s.strip()]
                if symbols else None)  # 夜间篮子的伪代码（night:memory:XKRX）区分大小写
        res = await run_replay(settings, MarketCalendar(), prices, days, syms)
        print(res.report(settings))
    except SourceError as exc:
        print(f"回放失败：取不到分钟数据。{exc}")
    finally:
        await prices.aclose()


async def cmd_check(settings: Settings) -> None:
    from tickersentinel.app import App
    from tickersentinel.timeutil import utcnow

    ok, bad, warn = "✅", "❌", "⚠️"
    u = settings.universe
    print(f"项目目录：{PROJECT_ROOT}")
    themes = "；".join(f"{th.name}（基准 {u.label(u.benchmark_for(u.theme_holdings(th.key)[0]) or '—')}"
                      + (f"，同业 {', '.join(th.peers)}" if th.peers else "") + "）" for th in u.active_themes)
    print(f"时区：{settings.timezone}｜标的：{', '.join(u.symbols)}｜主题：{themes}（tsen scope 看完整范围）")
    pc = settings.sources.prices
    has_alpaca = bool(settings.secrets.get("alpaca_api_key_id") and settings.secrets.get("alpaca_api_secret_key"))
    extras = []
    if pc.fallback != "none" and pc.fallback != pc.provider:
        extras.append(f"兜底 {pc.fallback}" + ("" if pc.fallback != "alpaca" or has_alpaca else "（缺 key，未启用）"))
    if pc.official_closes:
        extras.append("日线收盘价用 Alpaca SIP" + ("" if has_alpaca else "（缺 key，未启用）"))
    print(f"行情源：{pc.provider}" + (f"（{'，'.join(extras)}）" if extras else "") + f"｜推送渠道：{settings.notify.provider}")
    print("密钥：" + "  ".join(f"{k.upper()}{ok if v else '—'}" for k, v in settings.secrets.present().items()))
    print()

    async with App(settings, dry_run=True) as app:
        now = utcnow()
        # 所有会用到的代码都查一遍：监控标的、基准、同业、海外参照（主上市、汇率）
        benches = [m for b in u.benchmarks for m in (u.basket_members(b) or [b])]
        probe = list(dict.fromkeys([*u.symbols, *benches, *u.peers, *u.foreign_refs]))
        try:
            daily = await app.prices.daily_bars(probe, lookback_days=5)
            for sym in probe:
                df = daily.get(sym)
                if df is None or df.empty:
                    print(f"{bad} 日线 {sym}：无数据")
                else:
                    print(f"{ok} 日线 {sym}：最新收盘 {df['close'].iloc[-1]:,.2f}（{df.index[-1]:%Y-%m-%d}）")
        except Exception as exc:
            print(f"{bad} 日线：{type(exc).__name__}: {exc}")
        try:
            bars = await app.prices.intraday_bars(u.intraday_symbols, start=now - timedelta(days=4))
            for sym in u.intraday_symbols:
                n = len(bars.get(sym, []))
                print(f"{ok if n else bad} 分钟线 {sym}：最近 4 天 {n} 根")
        except Exception as exc:
            print(f"{bad} 分钟线：{type(exc).__name__}: {exc}")
        if has_alpaca and pc.provider != "alpaca" and (pc.fallback == "alpaca" or pc.official_closes):
            # 平时走主源，兜底坏了不会被发现：单独探测一次
            from tickersentinel.sources.prices_alpaca import AlpacaPriceSource

            syms = u.intraday_symbols
            for feed, what in (("iex", "实时兜底"), ("sip", "历史兜底 / 官方收盘")):
                src = AlpacaPriceSource(settings.secrets.get("alpaca_api_key_id"), settings.secrets.get("alpaca_api_secret_key"),
                                        feed=feed, timeout=settings.sources.http_timeout_seconds)
                try:
                    got = await src.intraday_bars(syms, start=now - timedelta(days=4))
                    print(f"{ok if got else warn} Alpaca {feed.upper()}（{what}）：最近 4 天 "
                          + "、".join(f"{s} {len(got.get(s, []))} 根" for s in syms))
                except Exception as exc:
                    print(f"{bad} Alpaca {feed.upper()}（{what}）：{type(exc).__name__}: {exc}")
                finally:
                    await src.aclose()

        q = app.news.build_query(u.symbols, now - timedelta(hours=48), now)
        for src in app.news.sources:
            try:
                items = await src.fetch(q)
                latest = max(items, key=lambda x: x.published_at).title[:50] if items else "—"
                by_sym = "、".join(f"{sym} {sum(sym in i.symbols for i in items)}" for sym in u.symbols)
                print(f"{ok if items else warn} 新闻源 {src.name}：48 小时内 {len(items)} 条（{by_sym}）｜最新：{latest}")
            except Exception as exc:
                print(f"{bad} 新闻源 {src.name}：{type(exc).__name__}: {exc}")
        if not app.news.sources:
            print(f"{warn} 没有启用任何新闻源")

        if app.events.sources:
            try:
                res = await app.events.refresh()
                for name in [s.name for s in app.events.sources]:
                    if name in res.errors:
                        print(f"{bad} 事件来源 {name}：{res.errors[name]}")
                    else:
                        print(f"{ok if res.per_source.get(name) else warn} 事件来源 {name}：{res.per_source.get(name, 0)} 条")
                mcfg = settings.events.macro
                if (mcfg.enabled and set(mcfg.us_data) & {"cpi", "ppi", "jobs", "jolts", "eci"}
                        and not settings.secrets.get("bls_user_agent")):
                    print(f"{warn} 事件来源 bls：未配置 BLS_USER_AGENT，CPI / PPI / 非农 / JOLTS 的发布日程没有收集"
                          "（BLS 要求请求头带联系邮箱，见 README）")
                today = now.astimezone(settings.tz).date()
                nxt = app.digest.window(today, 14)
                if nxt:
                    from tickersentinel.events.digest import event_line

                    print("  未来 14 天：\n" + "\n".join("  " + event_line(e, settings.tz) for e in nxt[:8]))
            except Exception as exc:
                print(f"{bad} 事件日历：{type(exc).__name__}: {exc}")
        else:
            print(f"{warn} 事件日历：没有启用任何来源")

        tasks = settings.llm.tasks.model_dump()
        print("大模型路由：" + "｜".join(f"{t} {app.llm.describe(t)}" for t in tasks))
        used = list(dict.fromkeys(n for names in tasks.values() for n in names))
        for name in used:
            provider = app.llm.providers.get(name)
            if provider is None:
                continue
            if not provider.configured:
                print(f"{warn} 大模型 {name}：缺少密钥 {settings.llm.providers[name].api_key_env}，用到它的任务会跳过")
                continue
            try:
                print(f"{ok} 大模型 {name}（{provider.kind}）{await provider.health()}")
            except Exception as exc:
                print(f"{bad} 大模型 {name}：{type(exc).__name__}: {exc}（用到它的任务会换下一个供应商或降级）")

    if settings.notify.provider == "feishu":
        try:
            from tickersentinel.notify import _build_remote

            notifier = _build_remote(settings, "feishu")  # 不走退回终端的逻辑：缺密钥要在这里报出来
            name = await notifier.bot_name()  # type: ignore[attr-defined]
            if name is None:
                print(f"{bad} 推送 feishu：拿不到机器人信息（检查 App ID / Secret，以及是否开通了机器人能力并发布版本）")
            else:
                print(f"{ok} 推送 feishu：机器人「{name or '（未返回名称）'}」，发往 {notifier.chat_id}"  # type: ignore[attr-defined]
                      "（用 tsen feishu test 发一条测试消息）")
        except Exception as exc:
            print(f"{bad} 推送 feishu：{exc}")
    elif settings.notify.provider != "console":
        try:
            from tickersentinel.notify import _build_remote

            _build_remote(settings, settings.notify.provider)
            print(f"{warn} 推送 {settings.notify.provider}：密钥已配置，但该渠道尚未实现（推送会失败，请改用 feishu 或 console）")
        except Exception as exc:
            print(f"{bad} 推送 {settings.notify.provider}：{exc}")
    else:
        print(f"{ok} 推送：console（打印到终端，并写入 {settings.notify.console_log_path}）")
    fs = settings.notify.feishu_sync
    if fs.calendar or fs.bitable:
        on = "、".join(n for n, flag in (("日历", fs.calendar), ("多维表格", fs.bitable)) if flag)
        if settings.secrets.get("feishu_app_id") and settings.secrets.get("feishu_app_secret"):
            print(f"{ok} 飞书同步：已打开（{on}）；权限与资源用 tsen feishu sync 检查")
        else:
            print(f"{bad} 飞书同步：打开了（{on}），但 .env 缺 FEISHU_APP_ID / FEISHU_APP_SECRET，运行时不会同步")
    else:
        print(f"{ok} 飞书同步：关闭（notify.feishu_sync）")


async def cmd_feishu(settings: Settings, action: str) -> None:
    from tickersentinel.models import Message
    from tickersentinel.notify.feishu import (
        FeishuNotifier, make_channel, post, start_long_connection, stop_long_connection,
    )
    from tickersentinel.timeutil import fmt_local, utcnow

    sec = settings.secrets
    app_id, app_secret = sec.get("feishu_app_id"), sec.get("feishu_app_secret")
    if not app_id or not app_secret:
        print("请先在 .env 里填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET（飞书开发者后台 → 凭证与基础信息）。")
        return
    if action == "test":
        try:
            notifier = FeishuNotifier(app_id, app_secret, sec.get("feishu_chat_id"), domain=settings.notify.feishu_domain)
        except ValueError as exc:
            print(exc)
            return
        now = fmt_local(utcnow(), settings.tz, "%m-%d %H:%M")
        mid = await notifier.send(Message(title="🧪 tickersentinel 测试消息", kind="info",
                                          body=f"**连接成功**（{now}）\n- 这是一条列表\n- 链接：[SEC EDGAR](https://www.sec.gov)"))
        await notifier.send(Message(title="", body="↳ 这是挂在上一条下面的回复（异动归因会这样显示）", reply_to=mid))
        print(f"已发送测试消息（message_id={mid}），请在飞书里查看。")
        return

    # setup：建立长连接，等你在飞书里给机器人发消息，打印该会话的 chat_id
    channel = make_channel(app_id, app_secret, settings.notify.feishu_domain)

    async def on_message(m) -> None:
        print(f"\n收到消息：{m.content_text[:60]!r}\n  会话类型：{m.chat_type}\n  chat_id：{m.chat_id}\n"
              f"  发送人 open_id：{m.sender_id}\n把这一行写进 .env：FEISHU_CHAT_ID={m.chat_id}")
        await channel.send(m.chat_id, post(f"这个会话的 chat_id 是 `{m.chat_id}`，把它填进 .env 的 FEISHU_CHAT_ID 即可。"),
                           {"reply_to": m.message_id})

    channel.on("message", on_message)
    try:
        await start_long_connection(channel)
    except Exception as exc:
        print(f"长连接建立失败：{exc}\n检查 App ID / Secret 是否正确、应用是否已开通机器人能力。")
        return
    identity = await channel.resolve_bot_identity()
    print(f"已连接飞书（机器人：{getattr(identity, 'name', None) or '未知'}）。保持这个窗口运行，然后：\n"
          "  1. 在开发者后台「事件与回调」把订阅方式设为“使用长连接接收事件”并保存，添加事件 im.message.receive_v1；\n"
          "  2. 发布版本后，在飞书里找到这个机器人给它发一句话（或把它拉进群并 @ 它）。\n"
          "这里会打印该会话的 chat_id。按 Ctrl+C 退出。")
    try:
        await asyncio.Event().wait()
    finally:
        print("\n正在断开飞书长连接……")
        await stop_long_connection(channel)


async def cmd_feishu_sync(settings: Settings, action: str, open_id: str | None = None, *, api=None) -> None:
    """飞书日历 / 多维表格。calendar-setup、bitable-setup：一次性建好并共享给你（可以重复运行，已有的不会重建）；
    sync：立即同步一次。用正式数据库（与 tsen run 共用；SQLite WAL 允许同时打开）。api 只供测试注入。"""
    from tickersentinel.feishu import FeishuError, FeishuSync
    from tickersentinel.storage import Storage

    storage = Storage(settings.resolve_path(settings.storage.sqlite_path))
    try:
        try:
            fs = FeishuSync.from_settings(settings, storage, api=api)
        except (ValueError, RuntimeError) as exc:
            print(exc)
            return
        try:
            await _feishu_sync_action(fs, settings, action, open_id)
        except FeishuError as exc:
            print("\n❌ " + exc.guidance())
    finally:
        storage.close()


async def _feishu_open_id(fs, override: str | None) -> str | None:
    from tickersentinel.feishu import FeishuError

    try:
        oid = await fs.user_open_id(override=override)
    except FeishuError as exc:
        print("⚠️ 找你的 open_id 时出错：" + exc.guidance())
        oid = None
    if oid:
        print(f"你的 open_id：{oid}")
    else:
        print("⚠️ 没找到你的 open_id，日历 / 表格照样会建好，但先只有应用自己能看到。补救：在飞书里给机器人发一句话后重新运行，\n"
              "   或者把 tsen feishu setup 打印的“发送人 open_id”用 --open-id ou_xxx 传进来。")
    return oid


async def _feishu_sync_action(fs, settings: Settings, action: str, open_id: str | None) -> None:
    from tickersentinel.feishu import FeishuError

    cfg = settings.notify.feishu_sync
    if action == "calendar-setup":
        oid = await _feishu_open_id(fs, open_id)
        cid, created = await fs.calendar.ensure_calendar(oid)
        print(f"✅ {'已新建' if created else '已有'}飞书日历「{cfg.calendar_name}」（calendar_id：{cid}）"
              + (f"，你是日历成员（{cfg.calendar_role}）" if oid else ""))
        res = await fs.calendar.sync(open_id=oid)
        print(f"   同步事件日历：{res.summary()}")
        if not res.created + res.updated + res.unchanged:
            print("   （数据库里还没有事件：tsen run 启动后会先刷新事件日历，打开 calendar 开关后自动同步过来）")
        print("下一步：\n"
              f"  1. 在飞书「日历」里订阅它：日历列表旁的「+」→「订阅日历」→ 搜索「{cfg.calendar_name}」→ 订阅。\n"
              "     不想订阅：把 settings.yaml 的 notify.feishu_sync.invite_user 改成 true，日程会作为邀请直接出现在你的主日历里（不发通知）。")
        if not cfg.calendar:
            print("  2. 把 settings.yaml 的 notify.feishu_sync.calendar 改成 true：常驻运行时每次刷新事件日历后自动同步。")
    elif action == "bitable-setup":
        oid = await _feishu_open_id(fs, open_id)
        await fs.bitable.ensure(oid)
        print(f"✅ 飞书多维表格「{cfg.bitable_name}」：{fs.bitable.url or '（链接见飞书「云文档 → 与我共享」）'}"
              + ("，你有完整权限" if oid else ""))
        res, _ = await fs.sync_alerts()
        print(f"   写入已有的异动：{res.summary()}")
        print("下一步：\n  1. 在「label 真实原因」「notes 备注」两列写下事后核实的原因；tsen feishu sync（或常驻运行时每小时）"
              "会把它们拉回 alerts 表。")
        if not cfg.bitable:
            print("  2. 把 settings.yaml 的 notify.feishu_sync.bitable 改成 true：常驻运行时每条异动、归因自动写进表格。")
    else:  # sync
        did = False
        if cfg.calendar or fs.calendar.calendar_id:
            did = True
            try:
                res = await fs.sync_calendar()
                print(f"✅ 飞书日历：{res.summary()}")
            except FeishuError as exc:
                print("❌ 飞书日历：" + exc.guidance())
        if cfg.bitable or fs.bitable.ids:
            did = True
            try:
                res, pulled = await fs.sync_alerts(pull_labels=True)
                print(f"✅ 飞书多维表格：{res.summary()}")
                if pulled is not None:
                    print(f"   拉回标注：{pulled.changed} 条有变化（表格共 {pulled.records} 行"
                          + (f"，{pulled.deleted} 行已被你删除，之后不再写入" if pulled.deleted else "") + "）")
            except FeishuError as exc:
                print("❌ 飞书多维表格：" + exc.guidance())
        if not did:
            print("还没有设置飞书日历或多维表格：先运行 tsen feishu calendar-setup / tsen feishu bitable-setup。")


async def cmd_ask(settings: Settings, question: str, keep: bool) -> None:
    """不加 --keep：用内存数据库，不留记录；加 --keep：用正式数据库里一个“终端”会话的记忆，可以连续追问。"""
    import time

    from tickersentinel.app import App
    from tickersentinel.agents.chat import IncomingMessage

    async with App(settings, dry_run=not keep, copy_db=True) as app:  # 预览也读得到正式库里的异动记录
        if not keep:  # 预览用内存数据库：先刷新事件日历，upcoming_events 工具才有数据
            await app.refresh_events()
        print(f"对话模型：{app.llm.describe('chat')}，思考中……")
        t0 = time.monotonic()
        answer = await app.chat.handle(IncomingMessage(text=question, chat_id="terminal"))
        print(f"\n{answer}\n\n（用时 {time.monotonic() - t0:.0f} 秒）")
        r = app.chat.last_result
        if r is not None:
            print("（工具：" + ("、".join(f"{c.name}{'(失败)' if c.is_error else ''}" for c in r.tool_calls) or "无")
                  + f"；调用模型 {r.rounds} 次，{r.provider}）")
