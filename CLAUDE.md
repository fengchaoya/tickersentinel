# CLAUDE.md — tickersentinel

> 写给 Claude Code 的项目说明：目标、已经定下的设计决策（含理由）、当前进度、开发约定。
> 修改设计时请同步更新本文件。和用户沟通用中文。

## 项目目标

按持仓 / 关注列表定制的盯盘与投研小助理（2026-10-21 由 memory-stock-agent 改名）。目标是通用：任何人换上自己的持仓配置就能用，
**代码和提示词里不要写死某个行业或某只股票**——持仓在 `config/assets.yaml`，每个板块的领域描述、基准、同业、夜间信号、相关公司、
检索词、板块指标在 `config/themes/<主题>.yaml`（决策 19），与具体股票无关的设置（基础宏观变量、宏观日程……）在 `config/settings.yaml`。
计划公开到 GitHub。命令行 `tsen`（= `tickersentinel`，也可 `python -m tickersentinel`），包名 `tickersentinel`。

开发和实测用的是一组存储芯片股：美光 **MU**、闪迪 **SNDK**、SK海力士 ADR **SKHY**
（2026-07-10 在纳斯达克上市，10 份 ADR = 1 股首尔普通股 000660.KS；主上市仍在首尔）。下文的实测数据和核实结论都基于这组股票。
仓库里的 `config/` 是通用示例（持仓示例 LITE + 光模块主题，存储芯片主题作为备用模块）；作者自己的配置放在不进 git 的 `config.local/`，
由 `.env` 的 `TICKERSENTINEL_SETTINGS=config.local/settings.yaml` 指过去（相对路径按项目根目录算，`config_paths`）。
测试读的是 `tests/fixtures/config/`（存储芯片这组的冻结副本，`conftest.TEST_CONFIG`），和示例配置互不影响。

1. **定时简报**：盘前 / 收盘 / 周报，推送到手机。
2. **全天候异动监控**：后台持续检测（默认每 60 秒），**只在出现急涨急跌时推送**；推送后自动检索新闻与讨论，给出最可能的原因，并附宏观快照。
   美股要 24 小时盯（用户 2026-10-21 要求）；每个资产的灵敏度不同（以后可能纳入黄金、白银），在 `config/assets.yaml` 一个文件里增删资产、调灵敏度。
3. **事件日历**（用户要求“提前收集，总结起来推送”）：持仓及相关公司的财报节点、大股东 / 内部人减持与解禁等，提前收集入库，每晚汇总推送，并写进简报。
   2026-10-21 用户补充：相关公司要包括云厂商（微软、谷歌……）和密切相关的公司（英伟达、AMD、台积电……），并加入宏观日程——
   美联储关键节点（议息会议、关键通胀数据的公布时间）等（见决策 17）。
4. **推送到 Telegram 或飞书**，并且能回复消息提问（双向对话）。
5. **大模型优先用本机**：开发机（Apple Silicon M5 Pro / 64 GB）跑 LM Studio + Qwen 27B 处理新闻；云端（Anthropic / OpenAI）留好接口，可以按任务切换。

读者（作者）：中文沟通，熟悉计量（事件研究、异常收益这类说法可以直接用）；本地时区 America/Chicago（settings.yaml 的 timezone）。

## 架构

```
src/tickersentinel/
├── cli.py / __main__.py     命令行：run / brief / events / tick / replay / check / ask / macro / assets /
│                            scope（show、suggest、add、remove、undo、check、tune、prompt）/
│                            feishu（setup、test、calendar-setup、bitable-setup、sync）
├── app.py                   组装所有模块；常驻运行入口；飞书同步的挂钩（见数据流）
├── config.py                settings.yaml + assets.yaml（资产清单与警报标准，merge_assets）+ themes/*.yaml（板块联动，merge_themes）
│                            + .env → pydantic 模型（写错字段名会直接报错）；Settings.rules_for(symbol) = 按资产的检测参数；
│                            Universe 从“持仓 × 主题”推出全部范围（benchmark_for / peers_for / sector_news / event_symbols……，决策 19）
├── models.py                NewsItem / Trigger / Alert / Message / Quote / Event（事件日历）
├── timeutil.py / textutil.py 时区换算与显示；URL 规范化、截断、去 Markdown
├── market_calendar.py       NYSE 交易日历（exchange_calendars，含节假日与提前收盘）
├── storage.py               SQLite：已读新闻、异动与归因（alerts）、简报、事件（events，记录新增 / 改期）、对话（chat_messages）、
│                            飞书同步映射（feishu_calendar_events、feishu_bitable_rows）、kv（含 feishu:* 键：日历 / 表格 ID、
│                            用户的 open_id、共享标记、日历“代数” feishu:calendar_gen:*）；in_memory_copy 供 tsen ask 预览
├── sources/                 所有网络调用都在这里；失败抛 SourceError
│   ├── base.py              PriceSource / NewsSource / EventSource / OptionsSource 接口
│   ├── http.py              共享的 HTTP 客户端（超时、User-Agent、重试）
│   ├── macro.py             宏观快照：yfinance 分钟线 + 财政部收益率曲线（决策 15）
│   ├── peer_candidates.py   联动候选：Yahoo 相似股票 / 行业龙头 / 批量报价核对代码、Finnhub 同业（决策 19）
│   ├── prices_yfinance.py   行情主源（零配置）；海外代码（.KS/.T/汇率）固定走这里。报价（build_quotes，Alpaca 共用）的
│                            “较昨收”按最新分钟线的美东日期取参照（不按现在的日期：过美东午夜、周末、节假日上午口径不变）
│   ├── prices_alpaca.py     Alpaca（实时 IEX / 15 分钟以前的 SIP）+ RoutedPriceSource 按代码路由
│   ├── prices_routing.py    FallbackPriceSource（主源 + 兜底）、OfficialCloseSource（日线收盘价换成 SIP），见决策 18
│   ├── news_finnhub.py      公司新闻 + 财报日历（有 key 时）
│   ├── news_google_rss.py   Google News RSS（英/韩/中三个版本）
│   ├── filings_sec_edgar.py SEC 公告（8-K / 6-K / Form 4 …）；受理时间取自每份公告的 index-headers；新闻与事件共用一个实例
│   ├── events_earnings.py   财报日：yfinance（全部代码；美股按 Yahoo 的“是否估计”标记，海外一律算估计）+ JPX 決算発表予定日（.T，官方）
│   ├── events_sec.py        Form 144 内部人拟减持；外国发行人 6-K 里的业绩发布时间（SKHY）
│   └── events_macro.py      宏观日程：美联储 / BLS / BEA / Census / 财政部拍卖 / 日本央行（官方日程）+ 按规则推算
│                            （美股休市与期权到期、韩国出口），见决策 17
├── events/                  hub.py 收集 → 归并到监控标的 → 多来源取最可靠 → 入库；digest.py 每晚汇总与排版
├── news/                    hub.py：NewsHub 并发聚合；dedupe.py：去重（URL 规范化 + 标题相似度；公告只按 URL 去重）
├── scope/                   监控范围：view.py（tsen scope 的文字视图）、discover.py（找候选 + 打分 → Proposal）、
│                            edit.py（写入 assets.yaml / themes/，备份、验证、撤销；改动前回放预览）、health.py（联动体检）、
│                            tune.py（调阈值：按日波动建议 + 回放对比）、chat.py（ScopeChat：飞书 /scope，待确认的改动存 kv）
├── detect/
│   ├── clock.py             交易时段切段：美股 盘前 / 盘中 / 盘后、CME 期货、UTC、海外交易所（见决策 16）
│   ├── basket.py            同业等权篮子（主题 benchmark: basket）：分钟线 / 日线同一口径，昨收 = 100
│   ├── baseline.py          每段开始时估计 β、σ、成交量画像（估计窗口）；参照价可由调用方给定
│   ├── price.py             异动规则（纯函数，无状态）；按资产的检测参数；盘前盘后 3 根中位数去错价
│   ├── cooldown.py          冷却 / 升级 / 全天档位去重（有状态，可从数据库恢复）
│   └── options.py           期权异动（占位）
├── llm/                     base.py 供应商接口（complete / complete_with_tools）与工具执行 run_tool_calls；
│                            client.py 按任务路由（local → 云端兜底）；openai_compat.py（LM Studio / OpenAI 等）、
│                            anthropic_api.py（Claude，可联网检索）、lmstudio.py（自动 lms server start / load）；prompts.py
├── agents/                  brief.py 简报、attribution.py 归因、render.py 排版、chat.py 对话（第二版：可调用查询工具）、
│                            chat_tools.py 5 个只读查询工具（ChatTools 定义工具；ToolSession 一次提问一个，新闻 [n] 编号、链接由程序替换）
├── notify/                  Notifier 接口 + 渠道开关；console、feishu 可用（lark-oapi FeishuChannel），telegram 占位；
│                            飞书 / Telegram 缺密钥或没装 SDK 时警告并退回 console
├── feishu/                  飞书日历与多维表格同步（推送 / 收消息在 notify/feishu.py）
│   ├── client.py            开放平台接口薄封装（lark-oapi 类型化请求）+ 错误码 → 中文处理建议；find_user_open_id
│   ├── calendar_sync.py     events 表 → 应用自建的飞书日历
│   ├── bitable_sync.py      alerts 表 → 多维表格；用户填的 label / notes 拉回 alerts
│   └── sync.py              FeishuSync：App 与命令行共用的入口（找 open_id、失败退避、错误提醒、退出时 drain）
└── jobs/                    monitor.py 全天候检测（按钟分组，prepare_context 供回放共用）、scheduler.py 日程规划、
                             replay.py 按段回放、calibrate.py（tsen assets：阈值换算成几倍 σ）、merge.py（同主题合并推送）
```

数据流：
- **简报**：Planner 定时 → BriefBuilder：行情快照 + NewsHub（新闻/公告，去重、过滤已读）+ 事件日历（events 表）+ 当日异动回顾 → LLM 撰写 → Notifier → 标记已读
- **事件日历**：Planner 每天 06:40 / 18:30 → EventHub.refresh：各 EventSource → 归并 → `Storage.upsert_event`（识别新增 / 改期 / 确认）；20:00 → EventDigest：明天的事件 + 新增变更 + 未来 14 天 → 有内容才推送
- **异动**：一个全天候常驻任务每分钟 → IntradayMonitor.tick：对每个钟（美股 / 各海外交易所 / CME / UTC）取当前段 → 段变了就 `prepare_context`（基线；美股盘中带上当天盘前的高低点）→ 这一段的分钟 K 线 → `session_frame` 对齐（盘前盘后再 `despike`）→ `PriceAnomalyDetector`（按资产的检测参数）→ `_admit`：`AlertGate` 冷却（key = 段的 key）、逐条入库 → 各组都检测完后 `_push`：取一次宏观快照 → `Merger.plan`（同主题同方向合成一条；merge_minutes 内跟着动的挂在第一条下面作回复）→ 推送 → 后台 `Attributor`（合成的一条归因一次）→ 以“回复”形式推送归因
- **对话**：飞书长连接收到消息 → 先回 `chat.ack` → `ChatAgent.handle`：`/new`、`/macro`、`/scope`（`ScopeChat`，不经过大模型）→ 其余：
  持仓 + 读者回复的那条推送 + 最近几轮对话 → `LLMClient.complete_with_tools`（工具由 `ToolSession` 执行）→ `link_refs` 把 [n3] 换成链接 → 回复；问答存进 `chat_messages`
- **监控范围**：`tsen scope add` / 飞书 `/scope add` → `scope.discover.propose`（候选 + 打分；大模型提名的候选来自 Claude Code 写的 `data/scope_candidates/`）→ 读者确认 → `ScopeEditor.apply`（备份、写入、验证）
  → `App.apply_scope`（热更新）；`monitor_tick` 前 `maybe_reload_scope` 发现配置文件变了也走 `apply_scope`
- **飞书同步**（`notify.feishu_sync`，默认关闭；`dry_run` / 预览从不构造 `FeishuSync`）：`App.refresh_events` / `send_event_digest` 刷新事件之后 → 同步飞书日历；每次 `monitor_tick` 之后 → 后台同步最近 3 天的异动到多维表格（上一轮没跑完就跳过）；看门狗每 15 分钟 → 全部异动对一遍账，并最多每小时把 label / notes 拉回 alerts

## 关键设计决策

1. **只在异动时推送**（用户明确要求）。没有异动就没有消息。检测频率、冷却、合并推送在 `config/settings.yaml`，各资产的阈值在 `config/assets.yaml`（和主题文件里夜间信号的 alert）。
2. **检测规则**（`detect/price.py`，2026-10-24 按用户要求改写：要报的是“日内急涨急跌”——“开盘涨 2% 再跌到 -1%”这种 3 个点的回落要推，
   原来的“较昨收关口 + 固定窗口端点涨跌”抓不到）。幅度一律按**参照价的百分点**算（+2% → -1% 是 3 个点，和读者的说法一致）：
   - A「急涨急跌」rapid：最近 w 分钟（5/15/30/60）里的**最高点到现价**的跌幅（或最低点到现价的涨幅）**同时**满足百分比下限和 z 值阈值。
     百分比保证“幅度够大”，z 值（r / (σ_1m·√w)，r 是从高低点算的对数收益）保证“相对近期波动异常”：高波动的日子自动收紧
     （10-24 实测 SNDK 近 5 天波动大，60 分钟档实际要 3.9% 才推）。用 √w 而不是“离高点几分钟”标准化：窗口内从最高点的回撤在随机游走下
     与窗口内最大涨幅同分布（反射原理），按窗口长度标准化含义稳定；按离高点的时间会高估刚刚形成的回撤。窗口不早于开盘 / 复盘，
     且至少攒够最短一档的分钟数（盘后段没有预热，开头 1 分钟的跳动不算）。两个方向都够时取起点更近的那一段（正在走的这一波）。
     17 天研究（10-01～10-23 盘中）：只把端点换成高低点、窗口不变时几乎没差别（19 → 20 条），关键是加 60 分钟一档、30 分钟降到 3%；
     “先到 ±1.5% 再反向走 3%”的冲高回落 / 探底回升 19 次，原规则只推了 8 次。
   - B「全天累计」day（取代原来较昨收的「关口」）：从“参照价（盘前 / 盘中 = 昨收，盘后 = 今日收盘，期货 = 上一交易时段结束）、
     同一 key 更早那一段（盘中带上当天盘前，`SessionContext.carry` 由 `prepare_context` 用 `day_extremes` 算）和这一段的最高点里最高的那个”
     算起的跌幅（涨的方向对称：最低的那个）越过 `day_pct` 的档位（默认 5/8/12%）时推送；起点取第一次出现的极值（同价位再碰到不算新起点）。
     carry 和盘前检测用完全同一套算法（session_frame 网格、3 根中位数、第一次出现），开盘后的起点时间和盘前推送时一致，推过的档位不重推
     （2026-10-24 审计实测：盘前成交稀疏时两边起点差了 19 分钟，开盘重推了一次）。
   - 拆分（板块 / 个股特异、同期涨跌）和触发同一口径：按各自参照价的百分点；基准、同业按标的最新一根 K 线对齐（`align_to`：
     截至此刻的最新价）——否则盘前盘后基准成交稀疏时，它的“最近 w 分钟”停在自己最后一笔成交，拆分错位（审计实测 -1.04% 的假板块贡献）。
     用户要求慢速的一路下跌也要推（“一路跌了 7、8 个点也应该推”），同时认为有了它“较开盘价的关口”可以删掉——关口其实一直是较昨收，
     现在由起点含昨收的全天累计接管跳空和单边。旧配置名 `levels_pct`（AlertRules）/ `day_levels_pct`（PriceRules）照样能读，规则名 level 只出现在旧记录里。
   - **阈值按资产设置**（`config/assets.yaml`，2026-10-21 用户要求：黄金白银和个股的波动尺度完全不同）：`defaults` 按类别，资产的 `alert`
     覆盖；`Settings.rules_for` 在 `monitor.price` 上叠加（`rapid_pct` 的键就是这个资产的窗口）。默认值（急涨急跌 1.5/2.5/3/3%、全天 5/8/12%，
     用户 10-24 定“先用 3% 看效果”）按日波动约 5% 的股票定；新增资产 / `tsen scope tune` 按“类别默认值 × 日波动 / ref_sigma_pct”换算
     （ref_sigma_pct = 这组默认值针对的日波动：股票、加密货币 5%，期货 2%；`scope/discover.py` 的 `suggest_rapid` / `suggest_day`，
     和默认差不到 10% 时照用默认），各资产用同样的“几倍波动”——纯计算加回放验证，不用大模型。`tsen assets`
     把阈值换算成几倍 σ。
   - C「个股特异」：剔除板块后的异常收益 AR = r − β·r_基准 显著（基准按主题，决策 19）。默认关闭；夜间篮子的成分只跑这一条。
   - 每段开头 10 分钟不跑 A/C（开盘竞价噪声大），跳空交给 B；盘后段例外（财报 16:05 发布，前几分钟正要盯）。午休复盘同开盘处理。
   - 每个标的每次检测最多一个 Alert，多条规则同时触发时合并展示。
   - 已知可以改进的：开盘后半小时的波动是全天均值的两三倍（U 形），σ_1m 用全天的 MAD，开盘时 5 分钟档偏灵敏（10 天里约 7 条“5 分钟内反弹 1.5 个点”
     出现在 09:40 前后）；可以按分钟位置给 σ 加日内画像。
3. **估计窗口 / 事件窗口**：β 用前 60 个交易日的日收益对板块基准回归（每只股票用自己主题的基准，存储芯片是 SOXX）；σ 用前 5 个交易日、会话内不重叠的 5 分钟收益做 MAD 稳健估计，再折算到每分钟（5 分钟收益可以减弱买卖价跳动和 IEX 稀疏成交的噪声）。开盘前估计一次，盘中不更新，避免异动本身污染基线。回放同样只用当天之前的数据，不存在前视偏差。
4. **冷却**（`detect/cooldown.py`）：急涨急跌 / 个股特异按“标的 + 规则 + 方向”计算；有效冷却期 = max(冷却期, 上次窗口, 本次窗口)，防止 60 分钟窗口在冷却结束后重复“看到”同一波下跌；幅度扩大到 2 倍以上可以突破冷却，推送时标注“扩大”（1.5 倍时一波大行情会连推三四条，10 天回放 14 条“扩大”→ 改 2 倍后 6 条）。全天累计按“段 key + 标的 + 方向 + 起点”：每档一次，起点换了（创出新高 / 新低）重新计；冷却按段 key 区分（盘中最后几分钟的急跌不再压掉盘后开头的财报急跌）。全天的档位在同一段里已有同方向、起点不早于它（同一波）、
   幅度 ≥ 这一档 × 0.75 的急涨急跌推过时不再单独推（`ABSORB`；和急涨急跌同一次检测一起推的除外）；这个判断只依赖入库的急涨急跌，重启前后一样。触发器的起点时间存进 `alerts.triggers_json`（`storage.trigger_to_json`），冷却状态可以从 alerts 表恢复，重启不会重复推送。
5. **两段式推送**：先发异动快讯（求快），归因完成后作为回复挂在快讯下面。
   - **同主题合并**（`jobs/merge.py` 的 `Merger`，`monitor.merge_minutes` 默认 10，2026-10-24 用户同意）：各组都检测完再推
     （`IntradayMonitor.tick` → `_admit` 逐条过冷却入库 → `_push`），同一次检测里同一主题（主主题；持仓、夜间信号、篮子都算，
     默认主题不合并）同方向的几条合成一条（`render_alert_group`），归因只做一次（`_attribute_group`：以动得最多的那只为主，
     其他几只写进提示词的“同一主题同时异动”，归因存进每一条）；第一条推出去之后 10 分钟内同主题同方向又触发的（别的股票跟上、
     或同一只扩大）挂在第一条下面作回复（`reply_to`），不再附宏观快照；个股因素为主（`sector_judgment`）的照样单独归因，
     其余的 attribution_text 记一句“同一波行情，原因见那一条的归因”。异动照样一条条入库（数据集不变），推送失败的逐条补发。
     跟帖的 attribution_json 写 `{"summary", "same_wave_as": 第一条的 id}`，`Storage.attribution_summary` 顺着取第一条的结论（对话工具、
     简报的异动回顾都能读到原因）；归因关闭时不写。合成一条归因时新闻窗口取全组的并集；提示词写明“这波涨跌的起点”，候选条目按
     “起点前后 / 检测前后”两个时间差标注。合并的串（`Merger.threads`）只在内存里，重启后 10 分钟内跟着动的会另起一条。`tsen replay` 报告合并后的推送条数
     （`count_messages`）：10-09～10-23 共 102 条异动 → 98 条推送，其中 19 条是回复。
6. **归因原则**（`llm/prompts.py`）：讲时间先后（异动之后才出现、只复述行情的报道不是原因）；先拆分板块与个股；找不到明确催化剂就直说；输出 JSON，便于入库和日后评估。候选新闻由我们提供并编号，模型引用编号，链接由程序替换，避免模型编造 URL。
7. **alerts 表是一个事件研究数据集**：记录每次异动的特征和当时的归因。预留 `label` / `notes` 两列，用于事后标注真实原因，评估归因准确率、校准阈值。
8. **推送渠道开关**：`notify.provider: console | telegram | feishu`。两个渠道都要实现（用户要求），**用户优先飞书**（日历、表格等工具更强：事件日历同步成飞书日程、异动写进多维表格，见决策 14）。两者都不需要公网地址：Telegram 用长轮询，飞书用长连接。
    - 缺密钥或没装 SDK 时 `build_notifier` 打一条警告并退回 console（只打印到终端），不让 `tsen run` / `--send` 启动就崩溃——仓库要公开，克隆的人多半没有飞书凭证；启动消息的“推送渠道”按实际使用的显示，退回时注明。
    - 飞书（`notify/feishu.py`，2026-10-21）：基于 lark-oapi 1.7+ 的高层封装 `FeishuChannel`，不自己拼底层接口——它负责 Markdown → 飞书富文本 post（粗体、列表、链接都能转）、超长自动拆分、可重试错误重试、长连接重连。消息发成 post（标题 + 正文），归因用 `reply_to` 挂在异动消息下。发送只走 HTTP，一次性命令也能推送；接收用长连接，SDK 在自己的后台线程 / 事件循环里回调，我们把处理交回主循环（大模型、数据库客户端绑定在主循环上），只处理 `FEISHU_CHAT_ID` 这个会话。国际版 Lark 设 `notify.feishu_domain: lark`（推送与日历 / 表格同步共用）。不要直接用底层 `lark_oapi.ws.Client`：它在模块导入时抓取事件循环并 `run_until_complete`，在已运行的 asyncio 程序里会冲突。
    - `tsen feishu setup` 建立长连接并打印发消息会话的 chat_id（飞书后台保存“长连接”订阅方式前需要连接已建立）；`tsen feishu test` 发测试消息与回复。
9. **免费数据源优先**。网络调用全部放在 `sources/`，必须设置超时，失败时抛 `SourceError`；上层负责降级，单个数据源失败不会让整体崩溃。
10. **时间处理**：内部一律用带时区的 UTC，只在展示时转换为本地时间。交易时段来自 exchange_calendars（含提前收盘），不要写死 15:15 这类时间；夏令时切换（2026-11-01）自动处理。
11. **没有可用的大模型也能跑通**：简报降级为标题列表，归因降级为“按时间接近程度列出新闻 + 板块拆分判断”，事件汇总只发列表。
12. **大模型：本地优先，按任务路由**（`llm/`，用户要求）。`llm.providers` 定义供应商档案（`openai_compat` 覆盖 LM Studio / Ollama / OpenAI / DeepSeek 等，`anthropic` 走官方 SDK）；`llm.tasks` 为 brief / attribution / events / chat 各列一串供应商，依次尝试，全失败才降级。默认全部 `[local]`。
    - **本地模型支持原生工具调用**（2026-10-21 实测）：LM Studio 的 OpenAI 兼容接口传 `tools`，qwen3.8-27b（reasoning_effort none）返回标准 `tool_calls`（finish_reason=tool_calls，arguments 为 JSON 字符串）；把结果以 role=tool + tool_call_id 发回后正常作答。两轮共约 5 秒。对话第二版直接用原生工具调用，不需要“JSON 调用计划”兜底。
    - **两种调用**（`llm/base.py`）：`complete` 一问一答（简报、归因、事件汇总）；`complete_with_tools` 带工具多轮（对话）。各供应商只实现协议格式（OpenAI 的 `tool_calls` / Claude 的 `tool_use` 块），工具的执行、出错、截断统一由 `run_tool_calls` 负责：一轮里的调用并发执行，每轮最多 `MAX_CALLS_PER_ROUND = 6` 个（防止模型一次请求几十个，多出的返回错误），每个结果最多 `MAX_TOOL_RESULT_CHARS = 6000` 字（本地模型上下文有限，长提示词也慢）；未知工具、参数不是 JSON 对象、工具抛异常都转成 `is_error` 的结果交给模型，不打断对话。
    - 工具循环：最多 `chat.max_tool_rounds` 轮（默认 4，取值 1–10），之后最后一轮不带工具、要求直接作答——OpenAI 兼容接口去掉 `tools` 并追加一句“工具调用次数已用完”；Claude 发 `tool_choice: {type: "none"}`（平时不传，即 auto）。不支持工具的供应商（没有覆盖 `complete_with_tools`，用基类默认实现；`supports_tools` 只是标记，路由不看它）不带工具直接回答，改用 `system_no_tools`（“查不了实时数据，不要编造”的版本）。路由器换供应商时从头开始（同一个 ToolSession；工具都是只读查询，重复执行无副作用）。
    - OpenAI 兼容：发回的 assistant `tool_calls` 里 arguments 统一 `json.dumps(arguments or {})`——模型偶尔写出坏 JSON（“{bad”、空串、数组），原样发回 LM Studio 报 500 / 400，整次对话失败（2026-10-21 实测）；原文只放在报错的 tool 消息里。Claude：整条 assistant content（含 thinking 块）原样发回，同一轮的 tool_result 放在一条 user 消息里；在工具调用中间被 max_tokens 截断时抛 LLMError 交给路由器（剩下的只是“让我查一下”之类的开场白，不能当回答）。对话不开联网检索。
    - **对话工具**（`agents/chat_tools.py`，全部只读）：`get_macro` 宏观快照（2026-10-21 用户要求替换掉个股最新价工具 `get_quotes`：个股价格在券商 App 里看更直观，真正需要查的是不方便找的宏观变量）｜`get_intraday` 今天（休市时为最近交易日）较昨收与最近 N 分钟的涨跌，拆成板块部分（β × 基准）和个股特异，列出同业；已收盘日用日线的官方收盘价（分钟线最后一根差约 0.02%）｜`search_news` 持仓 / 已知的美股代码走 NewsHub（预设的多语言检索词 + 公告；海外主上市代码先换成监控标的，000660.KS → SKHY），其他关键词和海外代码按语言交给 Google News；最多 `MAX_NEWS = 10` 条，其中给 SEC 公告留 `MAX_FILINGS = 4` 个名额（一天上百条新闻，Form 4 实测排第 89，只取最新 10 条时公告永远看不到）｜`recent_alerts` alerts 表与当时的归因｜`upcoming_events` 每行带代码和“（持仓）”标记（只有标题时模型把持仓 SK海力士当成同业），`days` 至少 14（模型常把“下周”当 7 天）、最多 `events.lookahead_days`；指定了 `symbol` 时直接查到收集范围（模型填 60 天时会漏掉 63 天后的 MU 财报）；查到边界时注明“日历只收集到 MM-DD，不要推测”，没查到且天数不够时提示可以调大 days（否则模型会编财报日期）；宏观日程每行以“宏观”开头，`category` 可只看 macro / company（指定 symbol 时只看公司事件）；问不在收集范围的代码（如 INTC）时注明“不能据此判断它没有财报”，没认出代码时注明列的是全部事件，超过 `MAX_EVENTS` 时注明只列了前几条。
    - 依赖缺失的工具不提供（没有宏观数据源就没有 `get_macro`，没有行情源就没有 `get_intraday`）；每个工具自己处理错误（参数错、数据源失败、60 秒超时都返回一句 `is_error` 说明）。`ChatTools.resolve` 把代码 / 名称对到代码：先精确匹配；部分匹配时中文 / 韩文名允许子串（“海力士”），英文只认 ≥4 个字母的整词——否则 ON、DIS、AD 这类真实代码会被当成 Micron、SanDisk、SK hynix ADR 的一部分。
    - 新闻编号 [n1] [n2]… 每次提问重新开始；`ToolSession.link_refs` 先去掉模型自己写的网址（Markdown 链接只留文字、裸网址删掉），再把编号换成链接，本次没出现过的编号去掉（模型会沿用上一轮的编号），相邻链接之间加顿号（否则在飞书里读起来像一个链接）。历史对话只存问题和最终回答。
    - **本地模型的知识会过时**（实测：qwen3.8-27b 不知道 SKHY 已于 2026-07-10 上市，曾质疑该代码是否存在）。所以 `prompts.reader_text` 明确“持仓与推送信息是最新事实，冲突时以它为准”，并支持 `universe.watchlist[].note` 写入模型不知道的背景。
    - 本机实测（2026-10-20，M5 Pro 64 GB，LM Studio 0.4.14，qwen/qwen3.8-27b 4bit MLX）：生成约 17 token/秒；3.9k token 提示词 + 540 token 输出约 41 秒；一次归因约 15 秒。
    - 必须 `reasoning_effort: none` 关闭思考：开着思考时要求 json_schema，模型会把 JSON 写进思考、耗尽 max_tokens、content 为空。LM Studio 的 response_format 只接受 json_schema / text。
    - LM Studio 按需加载用界面里的默认上下文（常为 4096，装不下简报），所以 `lmstudio.py` 在调用前主动 `lms server start` + `lms load -c 32768 --ttl 3600`；已加载但上下文偏小只告警、不重载。
    - 本地模型不能联网检索：归因只依据我们收集的候选新闻；提示词写成“有检索工具才检索”，同一份提示词对本地 / Claude 都适用。
    - Claude（Sonnet 5.5）：web_search_20260209；不能关闭思考，用 `effort` 控制；不发 temperature；max_tokens 16000；默认开 `fallbacks: "default"`（beta server-side-fallback-2026-07-01），被拒仍抛错交给路由器。OpenAI 推理模型要 `max_tokens_param: max_completion_tokens`。
13. **事件日历**（`events/`）：`Event.key` 稳定（财报 = 代码 + 日历季度，Form 144 = 原始 accession，手工录入可指定 id），同一事件改期时 key 不变，据此识别“改期”。多来源合并优先级：手工录入 > 已确认 > 有精确时间 > 来源可靠程度（公司 / 交易所公告 > Finnhub > yfinance，见 `models.EVENT_SOURCE_RANK`）。海外主上市代码的事件并入监控标的（000660.KS → SKHY）。每晚汇总只在“明天有事件”或“有新增 / 改期 / 时间变更 / 确认”时推送；首次运行发一份完整日历（加上近一周的拟减持）。日期按用户本地时区判断“明天”（SK海力士 10-28 09:00 KST = 10-27 19:00 CT）。
    - 收集范围 `events.lookahead_days` 默认 100 天（14–400）：季度财报间隔约 91 天，100 天保证每家的下一次财报都在库里，对话能答“下次财报哪天”（原先 45 天时约一半答不上）。每晚汇总只报 45 天以内估计日期的变化（`events/digest.py` 的 `CHANGE_HORIZON_DAYS`；已确认的不限）：Yahoo 对远期估计改得勤，逐条报“新增 / 改期”只会刷屏。
    - 防误报（`Storage.upsert_event`，2026-10-21 审查后加的）：已经过去的事件只静默更新（Form 144 除外）；已确认的不被估计覆盖；可靠程度更低的来源给出不同日期时忽略，除非原记录已 3 天没被原来源报告；估计日期要连续 3 天没被报告才删除（单个代码临时失败不会删了又当“新增”推送）；同一代码有确认的财报日时，前后 30 天内其他 key 的估计直接删掉（跨季度换 key）；手工录入以配置为准，改了删了会同步。
    - 防漏发：每 15 分钟一次看门狗（`App.catch_up` → `catch_up_events` / `catch_up_briefs`）——日历超过 12 小时没刷新就刷新；过了 20:00 四小时内、当天还没汇总也没判定“无须推送”就补发。00:05 的日程规划（只排简报）不设 misfire 宽限上限（`misfire_grace_time=None` + coalesce）；看门狗同样不设（macOS 上调度器的单调时钟休眠时不走，默认 1 秒宽限会让醒来后第一次触发被丢掉）。异动监控是一个全天候的 `monitor:tick` 常驻任务（宽限 None + coalesce + max_instances 1），不按天规划、醒来接着跑——原来按天排的监控任务在醒来晚于 06:05 时会整天缺失（审查实测），改成常驻任务后这类问题不复存在。
14. **飞书日历与多维表格同步**（`feishu/`，2026-10-21）：事件日历 → 飞书日历，异动 + 归因 → 多维表格（也是 alerts 表在手机上的标注界面，见决策 7）。
    - 开关 `notify.feishu_sync`，默认全关：`calendar`、`bitable`、`invite_user`、`calendar_name`、`calendar_role`（reader | writer，默认 reader）、`event_minutes`（默认 30）、`past_days`（默认 7）、`bitable_name`。只需要 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，与 `notify.provider` 无关。预览（dry_run，含 `tsen ask` 不加 `--keep`）从不同步：内存数据库里的映射写不回正式库，同步会重复建日程。
    - 权限：`calendar:calendar`、`bitable:app`（读写都够；缺权限提示先列这两个——先开只读权限的话，建日程 / 写记录又会报缺权限）；找 open_id 用 `im:message.p2p_msg:readonly`（推送那一步已开通），用成员列表核对另需 `im:chat:readonly`（可选，实测当前未开通）。开通后要发布新版本才生效；应用身份调用要求开通机器人能力（190007）。
    - SDK：lark-oapi 1.7.5 的类型化请求，用同步方法、放进线程执行（SDK 的异步方法获取 token 时仍是同步 requests 调用，会卡住主循环）。限流类错误重试两次；缺权限、凭证错误、日历 / 表格不存在属于 FATAL，整次同步停下。
    - 用户的 open_id（日历成员、表格协作者都要用）：命令行 `--open-id`（同时存进 kv）> kv `feishu:user_open_id` > 从 `FEISHU_CHAT_ID` 会话里找（`find_user_open_id`：最近 50 条消息里 sender_type=user 的发送人，再用成员列表核对；没有成员列表权限时只看发送人；本进程找过一次没找到就不再查）。群聊里多人发言或有多个成员时返回 None，要 `--open-id ou_xxx` 指定：找错人会把多维表格的完整权限和日历共享给别人。
    - **日历**：应用以自己的身份建日历（默认「tickersentinel · 事件日历」），公开范围 `show_only_free_busy`（飞书只能订阅 public / show_only_free_busy 的日历；同企业的其他人最多看到忙闲），把用户加为成员，默认 reader：日历是 events 表的镜像，在飞书里改了会被下一次同步覆盖，更正日期写进 `events.manual`。订阅只能用户自己在飞书里做（应用没法替用户订阅）；不想订阅就设 `invite_user: true`，把用户加为每个日程的参与人（不发通知）。日程一律“空闲”、不发通知、参与人不能改。有精确时间 → 定时日程（`event_minutes` 长）；只有日期 → 全天日程，结束日期不含当天（end = 次日，与 iCalendar 一致，larksuite/cli 读回时也减一天），不传时区。
    - 日历对账：同步今天之前 `past_days` 天起的全部事件；映射（事件 key ↔ 日程 ID + 内容哈希）在 `feishu_calendar_events`，按 calendar_id 区分（日历被删就新建，旧映射自然失效）。新 key 建日程、哈希变了更新、库里已经没有的 key 删除日程；更早的日程留着、不再更新。创建时带 idempotency_key（日历 + key + 哈希 + retry_tag + 代数），网络重试不会重复建；日程在飞书里被删（更新时报 `EVENT_GONE`：193000 / 193001 / 193003）用 retry_tag 重建；我们自己删的（事件从库里消失）把 kv `feishu:calendar_gen:*` 的代数加一，同一事件原样回来（手工录入注释掉又恢复、过时估计删了又报回来）时键不同，不会拿到已删除的那个日程而永远不显示。
    - **多维表格**：应用建多维表格（默认「tickersentinel · 异动记录」）和数据表「异动」，把用户加为协作者（full_access）；表格时区用 `settings.timezone`，百分比列存普通数字（格式 0.00，不乘 100），日期存毫秒。每条异动一行：入库后插入，归因完成、补发成功后更新；只更新程序维护的列，「label 真实原因」「notes 备注」只在插入时写一次（库里已有的标注），之后以飞书里的非空值为准拉回 alerts（见下一条）。映射（alert id ↔ 记录 ID + 内容哈希）在 `feishu_bitable_rows`，内容没变不写。batch_create 的 client_token 由“表 + 这批 alert id”的哈希生成、设上 UUIDv4 版本位（文档要求 v4 格式），同一批重试得到同一个值。
    - 拉回标注（`pull_labels`）：先读映射再检索——反过来的话，另一个进程（`tsen feishu sync` 与 `tsen run` 同时跑）在检索之后插入的行会被误判为已删除。飞书里的空单元格不覆盖库里已有的标注：列被改名 / 删掉后程序会补建一个空列，照搬空值会把标注全部清空（审查实测）；代价是想清空标注要在库里清。检索结果里没有的已映射行记为 deleted，之后不再写回（用户删掉的行不会“复活”）；但 `FRESH_ROW_GRACE`（10 分钟）内刚写入 / 更新过的行先不判定（飞书检索可能有延迟，误判是不可逆的；按 `feishu_bitable_rows.synced_at`）；批量更新报“记录不存在”时逐条重试，找出被删的那行。同一张表不能并发写（1254291）：同步与拉取共用一把锁（只在进程内）。
    - 失败处理（`sync.py` 的 `_guarded`）：同步失败只记日志，不影响推送和监控。整体失败和单条写入失败（例如有人把「时间」列改成文本，每次写入都报 1254060）都算失败：每一段连续失败只推送一次提醒（带开通权限的指引），之后 30 分钟（`RETRY_AFTER`）内不再尝试——盘中每分钟都会触发同步，不能每分钟撞一次同样的错；成功一次后重新计。
    - 退出：`App.aclose` 先设 `closing`（不再开始新的同步、不再推送失败提醒），`drain` 等正在跑的同步（看门狗直接调用的也登记在内）写完映射，30 秒超时就取消，然后才关数据库——否则映射写进已关闭的库会丢失、下次启动重复插入，还会推一条误报。
    - 状态：启动消息有一行“飞书同步：…”（打开了但缺密钥或没装 lark-oapi 时注明没生效）；`tsen check` 显示开关与密钥状态，权限和资源要用 `tsen feishu sync` 实测。
    - 日历只同步监控范围里的事件（`events.hub.event_in_scope`，和汇总、简报一致；移除主题后它的公司事件从日历撤掉，范围恢复后按代数重建）。
      多维表格的“较参照价%”按每条异动自己的参照价（另有一列“参照价”写明较昨收 / 较今日收盘 / 较上一交易时段）；写入时报“字段不存在”
      或表被删，清掉 `_ready` 缓存，下一次自动核对、补建，不用重启。
    - 已知限制：`invite_user` 从 true 改成 false 不会移除已加的参与人（内容哈希变了会逐个更新日程，但用户仍是参与人，日程还在主日历里）。

15. **宏观快照**（`sources/macro.py`，`settings.macro`，2026-10-21 用户要求）：附在每条异动推送末尾（`in_alert`）并交给归因模型，`/macro`（飞书，不经过大模型）、`tsen macro`、对话工具 `get_macro` 随时查。
    - 数据：yfinance 1 分钟线（`^TNX`/`^TYX` 收益率指数、`CL=F`/`GC=F`/`SI=F`/`ES=F`/`NQ=F` 期货、`^GSPC`/`^IXIC`/`^VIX`、`DX-Y.NYB`、`JPY=X`、`BTC-USD`；实测期货约延迟 10 分钟、利率与 VIX 约 15 分钟）+ 美国财政部每日收益率曲线 CSV（官方，不要 key，补 2 年期与 2s10s）。FRED 的 fredgraph.csv 现在有反爬验证，不带 key 取不到。
    - **涨跌只用分钟线算，不用日线**：期货换月时 Yahoo 日线和分钟线是不同合约（实测 CL=F 日线 101.79、分钟线约 98），外汇 / 比特币的日线日期切法也不同。`ref: close` = 上一个美股交易日最后一根（VIX 16:15 收盘，不能截在 16:00）；美股休市时股指换成 `off_hours_symbol` 期货，口径为“较美股收盘那一刻的期货价”；`ref: 24h` = 24 小时前（往回 asof，周一早上能找到周五）。近 1 小时变化：一小时前那根 K 线早于 70 分钟就不给（没在交易）。
    - 推送异动时最多等 `macro.timeout_seconds`（10 秒），取不到就不附；同一次检测里几只股票一起异动只取一次，另有 60 秒缓存。快照 JSON 存进 `alerts.macro_json`（`Storage._migrate` 给旧库补列）。

16. **全天候监控与切段**（`detect/clock.py`，2026-10-21 用户要求“美股应当 24 小时监控”）：每个资产按自己的钟切成“段”，段内复用原来的整套检测（`session_frame` 网格、前 5 个同类段估计 σ、全天累计按参照价）。
    - 美股：盘前 [04:00, 开盘)、盘中、盘后 [收盘, 20:00 美东)；盘前与盘中共用 key（同一个昨收；盘中带上盘前的高低点，盘前推过的全天档位开盘后不重推），盘后 key 加 `:post`、参照当天收盘（日线有当天就用官方收盘，否则取收盘前 30 分钟的分钟线）。`hours: regular` 的资产不检测盘前盘后。yfinance `prepost=True`；Alpaca 的分钟线本身含盘前盘后（未实测）。
    - **美东 20:00–04:00 的美股夜盘没有免费数据**：由海外同一家公司 / 同业接力——`holding: false` + `related_to` 的“只盯盘”资产（默认 000660.KS → SKHY，首尔时段 = 美东约 20:00–02:30）。推送标题标出名称并注明“SKHY 的夜间信号”，归因按 SKHY 检索新闻。实测 10-20 首尔 +8% → 10-21 SKHY 盘前 +4.7%。
    - CME 期货：美东前一天 18:00 到当天 17:00（周日晚的算周一），参照 = 上一段 17:00 的分钟价（不用日线：换月时是另一个合约）；UTC：自然日；海外交易所：exchange_calendars 常规时段（午休的处理见决策 19「午休」）。
    - **盘前盘后去错价**：用最近 3 根 K 线收盘价的中位数（`despike`）。实测 MU 10-20 17:21 ET 出现一根 896.07（正是当天官方收盘价，收盘集合竞价成交补报），前后都是 912–914，原逻辑推出“-2% 急跌”再“+2% 急涨”两条假警报。盘前盘后 yfinance 成交量都是 0，没法按成交量过滤。
    - 一组出错不影响其他组；alerts 表新增 `segment` 列（`Storage._migrate` 补列），`Alert.ref_label` / `segment_label` 决定“较昨收 / 较今日收盘 / 较上一交易时段”和标题里的【盘前】【盘后】【期货】。
    - 首次全时段回放（10-19～10-21，3 天）：24 条，其中盘前 11、盘后 2、首尔 8、盘中 3；盘前推送多在美东 04:00（隔夜跳空），本地时间是凌晨 3 点。用户决定**不设免打扰**：照常推送，免打扰由用户在飞书里自己设置（2026-10-21）。

17. **宏观日程与扩充的相关公司**（`sources/events_macro.py`，2026-10-21 用户要求）：
    - 相关公司（现在写在主题的 peers / catalysts 里，决策 19；和主题无关的才写 `events.related`；只收集财报日、不盯盘）扩到 16 家：云厂商（MSFT / GOOGL / AMZN / META / ORCL：资本开支 = AI 服务器与存储需求）、
      AI 芯片与代工（NVDA / AMD / AVGO / TSM）、存储同业（三星 / WDC / STX / 铠侠）、设备（LRCX / ASML）、终端（AAPL）。
    - 宏观日程（`events.macro`）：美联储（议息决议 + 记者会、标注点阵图；纪要；主席讲话与国会证词；褐皮书可选）、美国数据（BLS：CPI / PPI / 非农 /
      JOLTS / ECI；BEA：GDP / PCE；Census：零售销售 / 耐用品）、长端国债拍卖（10 / 20 / 30 年）、日本央行议息、韩国月度与前 20 天出口、
      美股月度期权到期（季月四巫日）、美股休市与提前收盘。都来自官方日程或交易所日历，不需要 key；BLS 例外——要求请求头带联系邮箱，
      所以单独一个 `BLS_USER_AGENT`（不复用 SEC 的那一行：把用户邮箱发给另一家机构要用户自己决定）。
    - 事件模型：`kind = "macro"`，`symbol` 是分组代码（FED / US / UST / BOJ / KR / MKT），key 按“数据所属期间 / 当年第几次会议 / CUSIP + 拍卖日”，
      改期时 key 不变、按“改期”提醒。BLS 的 ics 没有所属月份，按发布月推算（CPI / 非农 = 上月，JOLTS = 前两个月）；停摆打乱节奏时标题可能差一个月。
      日本央行决议时间不固定，按 12:00 JST 记并在标题注明“约”；韩国出口按惯例推算、标为预计；纪要没列进美联储日历的按会后 21 天估计。
    - 每晚汇总：“未来 14 天”分“宏观”“公司”两段；宏观的“新增 / 确认”不进“新增 / 变更”（提前几个月就排好了，首次上线时会刷屏），只报改期和时间变更。
    - 异动归因（`Attributor.nearby_events`）：把异动前 24 小时内的日程（有时间的按时间；只有日期的取前一天到当天，覆盖隔夜财报）和接下来 24 小时的
      日程写进提示词；提示词要求按先后判断，未来的日程只能解释为事前布局 / 避险、置信度最多“中”；evidence 可写“日程：事件名称”，排版时显示为文字。
    - 对话工具 `upcoming_events` 加 `category`（all / macro / company），`MAX_EVENTS` 提到 40。
    - 首次实测（10-21，`tsen events`，没配 BLS_USER_AGENT）：60 条（yfinance 22、fed 6、bea 8、census 3、boj 3、calendar_rules 17……），
      议息 10-28 13:00 CDT、GDP / PCE 10-29 07:30 CDT、日本央行 10-29 22:00 CDT、韩国 10 月出口 10-31 19:00 CDT，均与官方日程一致。

18. **行情源：yfinance 为主，Alpaca 兜底**（`sources/prices_routing.py`，2026-10-23 用户填了 Alpaca key 后实测改的）：
    - 原计划“实盘切到 Alpaca”放弃：免费账户的 IEX 盘前盘后只有美东约 08:00–17:00（04:00 的隔夜跳空要推迟到 08:00、17:00 后的盘后急跌
      盯不到），盘中 MU 每天 387–390 根、SKHY 317–375、SNDK 314–371（满额 390）；IEX 日线收盘价与官方差 0.01–0.08%。
      免费账户的 SIP 04:00–20:00 完整、日线收盘与官方一致，但最近 15 分钟查不了（“subscription does not permit querying recent SIP data”）。
    - 配置 `sources.prices`：`provider`（实时主源，默认 yfinance）、`fallback`（默认 alpaca：主源报错或缺代码时补；查 15 分钟以前的分钟线时
      用 SIP、更近的用 IEX）、`official_closes`（默认 true：日线 close 换成 SIP 的，只换两边都有的那几天，adj_close 和当天未收盘的行保留主源）。
      没有 Alpaca key 时两项都自动不启用。`AlpacaPriceSource(feed="sip")` 自己把 end 截到 15 分钟前。
    - **分钟线不混用两个来源**：价格逐分钟一致，但成交量口径完全不同（yfinance 盘前成交量全为 0，盘中 MU 是 SIP 的 2.7 倍），
      基线的成交量画像必须和实时成交量同源，所以只在主源整体失败或缺代码时才整段改用兜底。
    - 兜底只在状态变化时打警告（不会每分钟刷屏）；`tsen check` 单独探测 IEX / SIP，平时不用的兜底坏了也能发现。

19. **主题：板块联动做成可增删的模块**（`config/themes/<主题>.yaml`，2026-10-23 用户要求：“增加一个股票代码很简单，难的是和它
    相关的其他资产的联动”；基础宏观变量和具体股票相关的指标要分开）：
    - **监控范围 = 持仓（assets.yaml）× 它们的主题**，其余全部推出：`Universe.active_themes`（有持仓引用的主题）、`benchmark_for`、
      `peers_for`（同主题的其他持仓 + 主题同业）、`sector_news`（键 `SECTOR:<主题>`，`models.sector_tag / is_sector`）、`focus_text`、
      `event_symbols`（持仓 + 主上市 + 主题同业 + catalysts）、`instruments`（assets.yaml 的 + 生效主题的 overnight 自动生成的只盯盘资产，
      `origin="theme"`，`related_to` = 这个主题的持仓）。没有持仓引用的主题不生效（主题库里的备用模块）；卖掉最后一只持仓，
      这个主题的数据、事件、推送一起停（`EventHub.upcoming / changed_since` 只列范围内的公司事件，库里的数据不删）。
    - 主题字段：`name`、`focus`、`benchmark`（ETF / 指数，或 `basket`）、`peers`、`overnight`、`catalysts`、`news`、`indicators`、`note`；
      `peers / catalysts / overnight` 写列表或 `{代码: 名称}` 都行，`overnight` 还可以带 `alert` 覆盖阈值。资产用 `theme:`（或列表 `themes:`，
      第一个是主主题，决定基准）。没写 theme 的持仓归默认主题 `default`，由旧字段 `universe.focus / benchmark / peers / sector_queries`
      生成（旧配置不改也能跑）；仓库自己的配置已迁移成 `themes/memory.yaml`（另放了一个备用的 `themes/optics.yaml`）。
    - **四种联动分开放**（2026-10-23 用 LITE 实测）：同涨同跌的 `peers`（剔除 SPY 后的残差相关：COHR 0.86、AAOI 0.80）；
      `benchmark`（LITE 对 SOXX R² 0.40、对光模块篮子 0.71；SKHY 0.60 → 0.70）；夜间信号 `overnight`（亚洲当天涨跌与美股次日开盘跳空的
      偏相关，剔除美股前一天：中国光模块篮子 0.43，000660.KS 对 SKHY 0.74）；`catalysts`（NVDA 与 LITE 的残差相关只有 0.29，
      但它的财报牵动板块）。Yahoo 的行业分类给 LITE 配的是 CSCO、MSI、ZBRA，没用；“相似股票”COHR/AAOI/CRDO 之外还混了 NBIS、VRT。
    - **每只股票用自己的基准**：`MonitorGroup.benchmarks / peers / baskets`，`SessionContext.benchmark_for / peers_for`，
      `Alert.benchmark`（入库，`alerts.benchmark` 列由 `_migrate` 补）；同期对比只列自己主题的同业与基准。
    - **同业篮子**（`detect/basket.py`）：分钟线 = 100 × 成分“现价 / 当日昨收”的平均（同一交易日内前向填充），日线 = 成分日收益等权平均连乘、
      缩放到 anchor 之前的收盘 = 100，两者“较昨收”口径一致，β、全天累计、盘后参照都直接复用；只用美股同业（海外的交易时段对不齐），
      至少 2 只。篮子是伪代码 `basket:<主题>`，`MonitorGroup.fetch` 换成成分，永远不去数据源取。
    - **板块指标与基础宏观变量分开**：基础变量在 `settings.yaml` 的 `macro.indicators`；主题的 `indicators` 由 `MacroSource` 一起下载
      （`MacroQuote.theme`，入库键 `<主题>.<key>`），异动推送只附这只股票所属主题的（`render_theme_indicators`），`/macro` 单列“板块指标”。
    - 顺带修复：深交所 `.SZ` 原先映射到 exchange_calendars 里不存在的 `XSHE`（加任何深市股票都会报错），改为与上交所共用 `XSHG`；
      台湾上柜 `.TWO` → `XTAI`；`underlying_map` 不再包含夜间信号（否则中国光模块股的财报会被记到 LITE 头上）。
    - 首次实测（10-21～10-23 回放，临时加 LITE）：LITE 用篮子拆分（β 1.04）；三只 A 股夜间信号按默认阈值 3 天 49 条（同涨同跌、各推一条），
      所以 optics 模板给它们设了保守阈值（降到 21 条），以后可以改成按篮子触发。
    - **发现与修改**（`scope/discover.py`、`scope/edit.py`；`tsen scope suggest / add / remove / undo`）：出方案 → 读者确认 → 写入。
      候选：Yahoo 相似股票（v6 recommendationsbysymbol）、Yahoo 行业龙头（yf.Industry）、Finnhub /stock/peers（有 key）、大模型
      （`llm.tasks.scope`，json_schema：同业 / 海外 / 催化剂 / 检索词 / 板块指标）、已有主题里整理过的海外代码（大模型每次给的不一样）。
      代码先用 Yahoo 批量报价（v7 quote）核对：查不到的去掉；大模型的名称和 Yahoo 登记的对不上时并排显示（实测它把 2317.T 叫成瑞萨，
      Yahoo 登记的是 Systena）。打分（近一年日线）：同业 = 剔除 **SPY** 后的残差相关 ≥ 0.5（不用 QQQ：它由科技大盘股主导，剔除后
      AMD–AVGO 从 0.58 变 0.03、MU–WDC 0.47；不用等权 RSP：几乎剔不掉什么）；基准 = 行业 ETF 与篮子的 R²，篮子高 0.1 以上才用；
      夜间信号 = 偏相关 ≥ 0.3、样本 ≥ 60 天；大模型说是同业、残差相关却 < 0.35 的（NVDA 之于 AMD 0.14）放进催化剂；
      和已有主题成员平均残差相关 ≥ 0.6 就并入（0.5 时 AMD 会被并进存储芯片）。警报标准：按日波动 σ/5% 等比例换算（决策 2；原先是关口最低一档 ≥ 0.8σ，日波动 > 4.5% 时急涨急跌
      按 σ/4% 放宽、夜间信号波动 ≥ 3% 时急涨急跌 3 / 4.5 / 6%；夜间信号现在也按它们的平均日波动换算，和海外股票默认一样时不写）。
    - 写入：assets.yaml 用 ruamel.yaml（原样读写一字不差；追加一项并加标记注释）；移除只删那一项自己的行（缩进更深的续行），前后注释不动；
      新主题写成 `themes/<key>.yaml`（带生成日期和实测数字的注释）。`apply` 先备份到 `data/scope_history/`，写完 `load_settings`
      验证，读不通自动还原；`undo` 还原最近一次（删掉新建的文件）。`settings_after` 在临时目录里套用改动，用来预览范围变化和回放。
      `Settings.paths` 记住配置从哪读的（`load_from` 不看环境变量），写回同一处。manifest 记下写入后每个文件的 sha256：undo 前核对，
      那次改动之后文件又被改过（手工编辑、git pull）就拒绝，`--force` 才整份还原；命令行 undo 先预览、确认后才写。
      飞书 /scope 的待确认方案存成 JSON，`Proposal.from_dict` 把阈值的数字键还原（否则等于默认的阈值会被写死进 assets.yaml）。
      新主题里同一交易所的夜间信号合成篮子时，按波动换算的阈值写成主题的 `overnight_alert`（成分只跑“个股特异”，写在成分上不生效）。
      assets.yaml 的 defaults 里写错资产类别名直接报错。
    - **飞书 /scope**（`scope/chat.py` 的 `ScopeChat`，`ChatAgent.handle` 在大模型之前先交给它）：`/scope`、`/scope add|suggest|remove|undo|check`；
      改动先存进 kv（`scope:pending:<chat_id>`，30 分钟有效），读者回“确认 / 取消 / 去掉 X / 加上 X”时才拦截（没有待确认的改动时，
      “确认”“是”照常交给对话模型）。预览（dry_run，含 `tsen ask` 不加 --keep）只看不写。
    - **热更新**（`App.apply_scope`）：只换 `settings.universe`（assets.yaml + themes/；settings.yaml 的其他改动要重启），同时
      `news.universe`、`monitor.reload()`（重建分组和检测参数、清掉基线让下一次检测重估，冷却保留）、`events.update_scope()`（来源的
      公司名表、Form 144 范围）、宏观缓存失效，后台刷新一次事件日历。`App.maybe_reload_scope` 在每次 `monitor_tick` 前比较配置文件的
      mtime / 大小：变了就重读（命令行 `tsen scope`、手工编辑一分钟内生效，推一条“监控范围已更新”）；读不通只提醒一次、按原范围继续。
      只改了 settings.yaml（监控范围没变）时不重载、只提醒要重启；阈值变化按“现在的 monitor.price + 新的 assets / 主题”算，和实际生效的一致。
      热更新和正在估计的基线交错时（飞书 /scope 确认那一刻某组正在 prepare），按旧分组估出来的上下文不写回（`IntradayMonitor._gen`）。
    - **夜间信号按篮子触发**（2026-10-23 用户要求；存储芯片加了三星 005930.KS：对 MU / SNDK / SKHY 次日开盘跳空的偏相关 0.55 / 0.51 / 0.68，
      与海力士 0.60 / 0.54 / 0.68 相当，两者日收益相关 0.87）：同一主题、同一交易所、在盯盘的夜间信号 ≥ 2 只且 `overnight_basket`（默认开）时，
      生成伪代码 `night:<主题>:<交易所>` 的只盯盘资产（`Universe.night_baskets`，`clock_key` 按伪代码里的交易所切段），按篮子跑全部规则
      （阈值 = 主题的 `overnight_alert`，没写用海外股票默认）；成分的基准 = 篮子，`rules_for` 只开 `idio`（`PriceRules.rules`），
      门槛 `overnight_single_pct`（默认 3%），并要求成分自己也动了这么多（`IdioRule.min_raw_move_pct`）——只有两三只成分时，
      一只大涨、没动的那只相对篮子也会“走弱”同样幅度，不加这条会把没动的那只也推出去（测试实测）。
      海外篮子按交易所当地日期划分交易日（`add_baskets(..., tz=)`、`MonitorGroup.basket_tz`）：首尔时段跨美东午夜，按美东日期会被劈成两天、
      换两次昨收。`is_us_symbol` 对伪代码返回 False，`Universe.display` / `config.pseudo_label` 把伪代码换成名称显示。
      实测（10-12～10-23 回放）：首尔夜间推送 49 → 24 条；光模块模板的 A 股篮子 5 天 8 条。
    - **铠侠（285A.T）**（2026-10-24 用户要求加上）：对 MU / SNDK / SKHY 次日开盘跳空的偏相关 0.50 / 0.56 / 0.64，和首尔同一时段，
      但在另一个交易所（交易日历不同，分钟线对不齐），东京又只有它一只，所以单独触发。日波动 σ≈6.2%，阈值写在主题 overnight 里它的 `alert`
      （`tsen scope tune` 按 σ 建议；10-24 改规则后为急涨急跌 1.9/3/3.5/3.5%、全天 6/10/15%，10 天回放 26 条——东京时段起伏极大）。
    - **午休**（东京 11:30–12:30、上海深圳 11:30–13:00、香港 12:00–13:00；首尔、台湾没有）：`Session.break_start / break_end`（exchange_calendars），
      网格照样连续、午休里前向填充。检测时复盘当作开盘：复盘后 `open_warmup_minutes` 内不跑 A/C，窗口不跨午休（`since` = 复盘后的 K 线数）；
      σ 的 5 分钟收益按上午 / 下午分开算（`five_min_returns` 读 `frame.attrs["break"]`）。之前午休前后的价差被当成“5 分钟急跌”
      （铠侠 10 天 3 条，都在 12:30 复盘那一刻），午休里前向填充的 0 收益还把 σ 压低。
    - **调阈值**（`scope/tune.py`；`tsen scope tune 代码`、飞书 `/scope tune 代码 [全天 5 8 12] [急涨 60:3]`）：不给数字就按近 120 个交易日的
      日波动建议（按 σ/5% 等比例换算，规则同 discover），回放对比“原来 / 改后”推送量（`settings_after` 套用改动），
      确认后写入：持仓 → assets.yaml 那一项的 alert；夜间篮子（伪代码、主题名或任一成分）→ 主题的 `overnight_alert`；单只夜间信号 → 主题 overnight
      里那一项的 alert。只写改了的那几项（没改的不写死，类别默认变了还能跟着变）。主题文件也用 ruamel 读写（已把对齐用的多余空格去掉，
      原样读写一字不差；单只夜间信号的 alert 原地改，那一项的注释保留，列表写法会先换成 {代码: 名称}）。给数字时只改写出来的窗口
      （“急涨 60:4”，其他窗口不变），“60:0”去掉这一档；写在 assets.yaml 里的夜间篮子成分调的是篮子。`describe_change_effects` 里加了 `threshold_changes`：改了阈值（含手工改文件、热更新）会写明“MU：全天 5/8/12% → 6/10/15%”。
      10-24 规则改写后（决策 2）建议改为按 σ/5% 等比例换算急涨急跌和全天的档位；飞书里写“全天 5 8 12 急涨 60:3”，命令行 `--day` / `--rapid`
      （`--levels`、“关口”这些旧写法也认）。
    - **提名候选交给 Claude Code**（2026-10-23 用户要求：本地 27B 模型凭自己的知识提名代码太勉强；2026-10-24 用户决定：订阅额度不能用于
      API key、一年用不了几次，所以不让程序调用 API，而是新增资产时在 Claude Code 里用订阅额度准备）：项目技能 `.claude/skills/add-holding`
      → `tsen scope prompt 代码` 打印上下文（Yahoo 资料、相似股票、已有主题、提名要求、输出骨架、文件路径）→ Claude Code 联网核实后写
      `data/scope_candidates/<代码>.json`（格式同 `SCOPE_SCHEMA`）→ `propose` 自动读取（`load_candidates`；超过 30 天的不自动用，
      `--candidates 文件` 明确指定的照用），有候选文件就不调用大模型；飞书 `/scope add` 也读同一份。`tsen scope add` 不是交互终端时只预览、
      不写（`_confirmed`），加 `--yes` 才写；`--drop A,B` / `--keep C` 调整候选。`llm.tasks.scope` 默认 `[]`；`claude_research`（Claude Opus 5.5，
      effort high，web_search_20260209，每次约 0.2–0.5 美元）留作备用档，写进 `llm.tasks.scope` 才会调用（联网时不能同时用 output_config.format，
      靠提示词要求只输出 JSON + `extract_json`），还没真实调用过（缺 key）。
    - **联动体检**（`scope/health.py`）：同业平均残差相关 < 0.3、夜间信号偏相关 < 0.2、篮子比 ETF 的 R² 高 0.1 以上、Yahoo 相似股票里有
      ≥ 0.6 的新候选 → 建议。周报末尾附（`briefs.weekly.linkage_check`，只在有建议时附；测试夹具关掉，因为要联网查相似股票），
      `tsen scope check` / `/scope check` 随时查。只给建议，不自动改。实测（10-23）：存储芯片 WDC 0.66、000660.KS → SKHY 0.68，无建议。

## 数据源

| 用途 | 实现 | 状态 | 备注 |
|---|---|---|---|
| 分钟线/日线（主源） | yfinance | ✅ | 非官方接口；1 分钟数据只覆盖最近约 30 天 |
| 分钟线兜底、日线官方收盘 | Alpaca 免费账户 | ✅ | 2026-10-23 联网核实，见决策 18：IEX 实时但盘前盘后只有美东约 08:00–17:00、个股缺 5–20% 的分钟；SIP 完整、收盘价与官方一致，但只给 15 分钟以前的 |
| 海外参照 | yfinance | ✅ | 000660.KS、005930.KS、285A.T（铠侠）、KRW=X |
| 公司新闻、财报日历 | Finnhub | ✅ | 免费 key；实时新闻 WebSocket 要付费，所以用 REST 轮询；company-news 文档写“只覆盖北美公司” |
| 财报日（全部代码） | yfinance | ✅ | 不需要 key；美股用 Yahoo 的 dateisestimate 判断是否确认；海外代码一律算估计（000660.KS 的日期常差一天，发布后的“时间”是 Yahoo 入库时间，不采用）；主数据走 yfinance 的内部类 YfData，失败时退回 Ticker.calendar |
| 财报日（东京上市） | JPX 決算発表予定日 | ✅ | 官方 xlsx，运行时从页面找链接；记为已确认 |
| 宏观日程 | 美联储（fomccalendars.htm + json/calendar.json）、BEA（release_dates.json）、Census（经济指标日程页）、财政部（TreasuryDirect upcoming）、日本央行（议息日程页） | ✅ | 不需要 key，2026-10-21 联网核实；细节见 `sources/events_macro.py` 文件头 |
| 宏观日程（BLS） | bls.gov 发布日程 ics | ⚠️ | 要求 User-Agent 带联系邮箱（`.env` 的 `BLS_USER_AGENT`），没填就跳过 CPI / PPI / 非农 / JOLTS；只用占位邮箱测过解析 |
| 内部人拟减持 / 外国发行人业绩时间 | SEC Form 144 / 6-K | ✅ | 与新闻共用 SEC 客户端；Form 144 只看持仓；6-K 靠正则识别业绩发布 / 电话会时间 |
| 新闻（英/韩/中） | Google News RSS | ✅ | 不需要 key |
| 美股公告 | SEC EDGAR | ✅ | 不需要 key，但 User-Agent 必须带联系邮箱（放 `.env` 的 `SEC_EDGAR_USER_AGENT`）；受理时间不能用 submissions JSON，见「联网核实结论」 |
| 韩国公告 | OpenDART | ⏳ | 需要申请 key |
| 讨论 | Reddit / X | ⏳ | 只有归因改用 Claude（可联网检索）时才覆盖 |
| 期权 | 券商 API / Massive | ⏳ | 先做零成本的快照差分版，见 detect/options.py |
| 大模型（默认） | 本机 LM Studio | ✅ | qwen/qwen3.8-27b（4bit MLX，约 16 GB）；关闭思考；自动启动与加载 |
| 大模型（可选） | Claude API / OpenAI | ⏳ | claude-sonnet-5-5（web_search_20260209）/ gpt-5-mini；缺 key，未实测 |

## 当前进度（v0.1 骨架）

- **已实现并有离线测试**：配置、交易日历、存储、行情/新闻/公告数据源、新闻去重、基线估计、三条检测规则、冷却、回放、简报（含降级）、归因（含降级）、事件日历（财报日、Form 144、6-K、手工录入、宏观日程；每晚汇总）、大模型路由（本地 LM Studio / Claude / OpenAI 兼容，含工具循环）、对话第二版（5 个查询工具）、飞书日历 / 多维表格同步、console 推送（缺密钥时退回）、日程规划、命令行。`pytest -q` 全部离线。
- **占位（接口已定）**：Telegram（文件顶部有实现计划）、期权监控（第 4 阶段）、DART。
- **公开前审计（2026-10-24）**：按检测核心 / 配置与监控范围 / 其他模块 / 文档 / 公开发布五个维度审查，54 条发现，代码问题都已修复并补测试
  （冷却按段、吸收比较起点、盘前起点一致、基准对齐、tune 只改给出的窗口、undo 指纹、热更新竞态、跟帖归因、日历按范围……，
  细节已写进上面各条决策）；文档、注释与代码对齐，README 加了免责声明和快速开始。待作者决定的：许可证、提交作者邮箱、示例配置是否保留真实持仓。
- **飞书已联网验证（2026-10-21）**：长连接、推送、回复挂接都通；`notify.provider: feishu`。
- **监控范围（2026-10-23）**：主题结构（决策 19）、`tsen scope` 的发现 / 写入 / 撤销、飞书 `/scope`（确认后即时生效）、配置文件热更新、
  周报联动体检都已完成，离线测试 + 用 LITE / AMD 实测。还没在真实飞书里点过 `/scope add`（`tsen ask "/scope add LITE"` 走的是同一套逻辑）。
- **对话第二版**（`agents/chat.py` + `chat_tools.py`，2026-10-21）：飞书收到消息先回 `chat.ack`，再用 `llm.tasks.chat`（默认本地）带工具回答；上下文 = 当前时间与美股交易状态 + 持仓（含 `note`）+ 读者回复的那条推送 + 最近 `chat.history_turns` 轮对话（`chat_messages` 表）；`/new`（或 `/reset`、“新对话”）清空。实测（本地 qwen3.8-27b）：一次回答 18–50 秒，调用模型 2–3 次。`tsen ask` 在终端里测同一套逻辑，并打印用到的工具和模型调用次数；不加 `--keep` 时用 `Storage.in_memory_copy` 把正式库只读复制进内存（读得到历史异动和事件，写入不落盘），先刷新一次事件日历；加 `--keep` 用正式库和“终端”会话的记忆。
- **飞书日历 / 多维表格同步：只离线测过**。只用 `tests/test_feishu_sync.py` 的 FakeFeishu（替换 lark-oapi 的 `Transport.execute`）测过，还没有在真实飞书上建过日历和表格。上线步骤：用户开通 `calendar:calendar`、`bitable:app` 并发布新版本 → `tsen feishu calendar-setup` / `bitable-setup` → 在飞书订阅日历（或设 `invite_user: true`）→ 把 `notify.feishu_sync.calendar` / `bitable` 设为 true → 重启 `tsen run`。
- **已联网验证（2026-10-20）**：本地模型（LM Studio 自动启动 + 加载、json_schema 归因输出）。
- **已联网验证（2026-10-11，yfinance 1.7.0 / pandas 3.0）**：yfinance 日线与分钟线（全部 9 个代码）、Google News RSS（英/韩/中共 16 个检索）、SEC EDGAR（MU / SNDK / SKHY）、`tsen check`、三种简报的降级版、`tsen replay --days 10`。结论见「联网核实结论」。
- **已联网验证（2026-10-23）**：Alpaca 免费账户（IEX / SIP，决策 18）。
- **还没联网验证（缺 key）**：Finnhub、Claude API。填好 `.env` 后先跑 `tsen check`。

## 路线图

1. **推送阶段**：飞书已完成并联网验证；Telegram 之后再做。审计发现的可靠性问题已于 2026-10-21 修复：
   - 推送失败的异动：照常入库、冷却照常生效，但 message_id 为空表示未送达；之后每次检测都重发，30 分钟内送达就补上（标题注明“补发”），再晚放弃（`jobs/monitor.py` 的 `RESEND_WINDOW`）。
   - 错过的简报：看门狗补发——盘前简报只在开盘前补、收盘简报收盘后 6 小时内、周报当天 12 小时内；过了定时点 10 分钟才算错过；生成失败当天最多补 3 次。`App.run_brief` 每种简报每天最多一份（正在生成时也不会再起一份），所以看门狗和定时任务不会重复；`tsen brief --send` 用 force 照发。
   - 简报新闻窗口：盘前 / 收盘简报回看到上一份盘前或收盘简报（最多 96 小时），周末、节假日不再有空档；已读条目由 `storage.unseen` 过滤。
   - 启动消息、长连接启动失败不再让进程退出。
   飞书日历（事件日历同步成日程）和多维表格（alerts 表的人工标注）已于 2026-10-21 实现（决策 14），还差在真实飞书上跑一遍（见「当前进度」）。
   之后：`tsen check` 用只读 GET 探测日历 / 多维表格的权限；`tsen feishu sync` 与 `tsen run` 同时写同一张表时锁只在进程内，考虑加文件锁或给出警告。
2. **阈值校准**：规则已于 2026-10-24 改成“相对近期高低点 + 全天累计”（决策 2），默认值用户定为先用 3% 看效果。10 天全时段回放：
   新规则 102 条（盘中 42、海外 45、盘前 13、盘后 2），旧规则 110 条；持仓 71 → 57；铠侠 15 → 26（东京时段起伏极大，要不要放宽等用户决定）。
   同一主题同时触发的已合并推送（决策 5）。铠侠先维持现状（用户 10-24）；开盘半小时波动大，用户说照常报告（σ 日内画像暂不做）。
   行情源已定为 yfinance 为主、Alpaca 兜底（决策 18）。
3. **双向对话**：第二版（5 个查询工具，决策 12）已于 2026-10-21 完成并实测。之后 Telegram 渠道做好时复用同一个 ChatAgent。
4. **期权监控**：`detect/options.py`，先做快照差分版（MU / SNDK / SKHY 的期权都已上市且有流动性）。
5. 事件日历扩展：DART（SK海力士的大量持股 / 高管与主要股东交易计划报告 D005，需 key）、424B7 / S-3ASR 转售与锁定期条款自动提取、SKHY 的 ADS 存托上限变化。
6. 其他：归因准确率评估（利用 alerts.label）、简报里加入成交量和资金流指标、降级简报只显示前 5 条却把选中的条目全部标记已读。
7. 已知没修的小问题：报价的时段标签不认提前收盘日（11-27 半日市 13:00–16:00 ET 的分钟线仍标为 regular）；海外代码（.KS）在首尔交易时段也显示 closed（只有日线）；`invite_user` 从 true 改 false 不移除已加的参与人（决策 14）。

## 联网核实结论（2026-10-11）

1. **SEC 公告受理时间的时区** —— 原假设（“带 Z 后缀但实际是美东时间”）不成立，而且 submissions JSON 目前不能直接用：
   - 权威时间是每份公告目录下 `<accession>-index-headers.html` 里的 `<ACCEPTANCE-DATETIME>`，**美东墙上时间**，与索引页的 “Accepted” 一致（旁证：业绩 8-K 落在 16:02、EDGAR 06:00 开门时集中受理 6-K、17:30 后受理的公告 filingDate 记到下一个工作日）。
   - `data.sec.gov/submissions` 的 `acceptanceDateTime`：两轮独立抽样共对照 109 家公司。最近一份公告在 2026-10-02 及以前的 58 家（SKHY、ASML、STX 等）是**真 UTC**；最近一份公告在 2026-10-05 及以后的 51 家（MU、SNDK、WDC、AAPL 等；有新公告时整份文件会重新生成）整份文件——包括历史记录——都是**美东时间 + 2×偏移**（夏令时 +8h，冬令时 +10h），即比真 UTC 晚 4～5 小时。分界干净，同一份文件内偏移一致，没有反例；看起来是 SEC 一侧 10 月初的回归，截至 10-09 仍未修复，随时可能被修复。SKHY 下一次提交 6-K 时，它的文件也会翻到“多转一次”。
   - 旧代码按美东解析：MU / SNDK 的公告时间会晚 8 小时（MU 10-K 实际 10-09 08:21 ET 盘前受理，会被当成 16:21 ET 盘后），SKHY 晚 4 小时，足以颠倒“公告在异动之前还是之后”。
   - 现在的做法（`filings_sec_edgar.py`）：用 JSON 时间 ±12 小时粗筛（不用 `filingDate`：它会顺延到下一个工作日，圣诞连休时实测晚 6 天）→ 逐份读取 `<ACCEPTANCE-DATETIME>`（按 accession 缓存；请求间隔 ≥0.15 秒，守住 SEC 的 10 次/秒）→ 读取失败时才用 JSON 时间，偏移依次取自：本次从同一份文件对照出的值 → 不联网的判据（EDGAR 只在工作日 06:00–22:00 ET 受理，选让整份文件越界更少的偏移）→ 上次对照出的值 → 当作 UTC 并打日志。不写死任何偏移，SEC 修不修都不受影响。
   - 顺带修复：EDGAR 在 2024 年底把 `SC 13D/G` 改名为 `SCHEDULE 13D/G`，原来的配置匹配不到；各类型的 `/A` 修订现在也会带上；8-K 标题附带事项含义（`items` 字段，如 2.02 = 业绩发布）；公告标题按模板生成、彼此雷同，原先会被 NewsHub 的标题相似度去重合并（30 天 16 条只剩 7 条），现在公告只按 URL 去重。
2. **yfinance 1.x**：`285A.T`、`KRW=X`、`.KS` 都能取到日线；多代码下载（`group_by="ticker"`）的列层级是 (Ticker, Price)，`split_by_symbol` 两种层级都兼容；1 分钟线每个交易日 389～390 根，含盘前盘后的分钟线（04:00～20:00 ET）解析正常。周末 `KRW=X` 的日线会多出一行当天日期的报价。
3. **SKHY 期权**：已上市，有周度到期（18 个到期日）；10-16 到期一档成交量约 2.7 万张、未平仓约 16 万张，前 8 个到期日未平仓合计约 64 万张（MU 同口径约 68 万张），平值买卖价差约 0.1 美元。第 4 阶段可以监控 SKHY 期权。
4. **SKHY 的 ADR 比例与溢价**：招股书（424B4）确认 1 份 ADS = 1/10 股普通股，`adr_ratio: 10` 正确。但 ADR 相对首尔股价长期溢价：上市以来均值约 +36%（区间 +15%～+63%）。原先盘前简报里的“ADR 隐含价（较昨收 x%）”因此恒为 -25% 左右，没有信息量；2026-10-20 改为显示“折合价与 ADR 溢价”。
5. **Google News RSS**：三个版本、16 个检索全部正常，时间为 UTC。已知噪声：中文检索词（如“闪迪”）会混入不相关条目；个别条目的来源名是网址；单次检索最多返回 100 条，周报窗口（`when:8d`）会被截断。有大模型时由模型筛选，降级模式下会直接列出来。
6. **首次回放**（`tsen replay --days 10`，09-28～10-09，当前阈值，yfinance 分钟线）：共 30 条，日均 3 条，7/10 个交易日有推送，单日最多 8 条（10-08）；MU 9 / SNDK 11 / SKHY 10。24 条只触发关口规则，另有 1 条关口与急跌合并（±3% 关口 18 次、±5% 关口 7 次）；急涨急跌共 6 条（z 值 7.5～11.4，实际起约束作用的是百分比下限而不是 z 阈值）；16 条发生在开盘后 90 分钟内。阈值尚未调整。
7. **回放中发现的检测问题（已修，a00ee7a）**：冷却把急涨急跌触发过滤掉、只剩关口触发时，Alert 的拆分上下文仍是被过滤掉的那个窗口的（09-28 09:13 SNDK）。现在每个触发口径各算一份拆分，过滤后随标题切换。

## 仍待核实（需要 key）

没有 key，只能对照官方文档（Alpaca 文档站、Finnhub 的 swagger.json）：两个解析器的端点、参数、字段、分页、日 K 时间戳都与文档一致，无凭证请求返回 401 并被转成可读的 SourceError。拿到 key 后先跑 `tsen check`，重点看：

- **Alpaca**：2026-10-23 已用免费 key 实测（结论见决策 18）。还没覆盖到的：IEX / SIP 日线没有复权（`adjustment=raw`），
  只在兜底时才会用到它的整份日线，遇到拆股要留意。
- **Finnhub 是否覆盖 SKHY**：文档写 company-news “只覆盖北美公司”，没说美国上市的外国 ADR 算不算；财报日历可能只挂在 `000660.KS` 下（需要 `international=true`）。不覆盖时表现为 0 条而不是报错。
- **Claude / OpenAI**：`claude-sonnet-5-5` + `web_search_20260209` + `fallbacks: "default"`，以及 OpenAI `gpt-5-mini` 的实际调用（`tsen check` 会对 llm.tasks 用到的每个供应商做一次连通测试）。Claude 的工具循环只用假的 SDK 客户端（`tests/test_llm.py` 的 `FakeAnthropic`）测过，请求形状没经过真实 API 校验。
- **飞书日历 / 多维表格**（不需要新 key，需要开通权限）：请求字段、枚举、响应键已对照 lark-oapi 1.7.5 源码，但没在真实飞书上跑过。重点看：全天日程的结束日期是否确实不含当天（目前依据 larksuite/cli 的处理推断）；订阅后日程详情是否可见；batch_create 之后马上检索能否检到新行（索引有延迟的话会被误判为已删除）；idempotency_key / client_token 的去重时效（文档没写）。

## 开发约定

- Python ≥ 3.10，src 布局；环境用 uv：`uv venv --python 3.12 && uv pip install -e ".[dev]"`（虚拟环境在 `.venv/`，命令前加 `.venv/bin/` 或先 `source .venv/bin/activate`）。
- 测试：`pytest`（asyncio_mode=auto）。**测试全部离线**：`tests/conftest.py` 提供合成的单因子市场（r_i = β·r_m + ε，可注入冲击）和假数据源；
  `settings` 夹具关掉了 `events.macro`（宏观日程的来源会联网），宏观日程的测试用 `tests/fixtures/macro/` 里截取的真实文件。
- `tests/test_feishu_sync.py` 需要先装飞书 SDK（`uv pip install -e ".[dev,feishu]"`）：没装时 `pytest.importorskip("lark_oapi")` 会把整个文件静默跳过（只显示 s，不报错），改了 `feishu/` 却以为测试通过了。
- 修改检测逻辑必须补测试；调整阈值前先用 `tsen replay` 看推送数量的变化。
- 资产清单与警报标准只在 `config/assets.yaml`（`settings.yaml` 里不要再写 `universe.watchlist`，两处都写会报错）；板块联动只在
  `config/themes/`（自己的配置在 `config.local/` 里同样分三处）。测试的 `settings` 夹具读 `tests/fixtures/config/`（冻结副本：
  改了示例配置不影响测试；要改测试用的配置就改那一份）；只想留几个资产时用 `conftest.isolate(settings, symbols)`（同时清掉主题同业、
  只留 symbols 里的夜间信号）。新增资产类别：`AssetClass` + `default_class_rules` + `detect/clock.py` 的 `clock_key` / `build_clock`。
- 代码里要“这只股票的基准 / 同业 / 板块新闻 / 领域”时，一律用 `Universe.benchmark_for / peers_for / sector_news / focus_text`，
  不要用全局的 `universe.benchmark`（那只是没写 theme 的资产的兜底）；取行情前把 `basket:<主题>` 换成成分（`basket_members`）。
- 新增数据源：实现 `sources/base.py` 的接口 → 在 `sources/__init__.py` 注册 → 用 `httpx.MockTransport` 写解析测试。
- 新增推送渠道：实现 `notify/base.py` 的 `Notifier` → 在 `notify/__init__.py` 注册。构造时缺密钥 / 没装 SDK 抛 `ValueError` / `RuntimeError`，`build_notifier` 会警告并退回 console，不要让它崩溃。
- 新增对话工具：在 `ChatTools.specs` 加一个 `ToolSpec`（JSON Schema；依赖缺失时不提供）→ 实现方法（放 `ChatTools` 或 `ToolSession`；要给新闻编号的必须放 `ToolSession`）→ 在 `ToolSession.execute` 的 handlers 里登记。必须只读；参数错抛 `ValueError`、数据源错抛 `SourceError`，由 `execute` 统一转成 `ToolOutput(is_error=True)`，不能让异常打断对话；结果用紧凑 JSON 或短文本，时间换成读者本地时区；代码用 `resolve` 解析，标的、名称都来自 `settings.universe`，不写死。需要模型遵守的用法写进 `agents/chat.py` 的 `chat_system`。测试放 `tests/test_chat_tools.py`。
- 以 `_pct` 结尾的字段都是百分数（-2.8 表示 -2.8%）。
- 密钥只放项目根目录的 `.env`（不进 git；提交到 git 的是空白模板 `.env.example`，仓库要公开到 GitHub，模板里绝不能出现真实值）；不要把用户邮箱等个人信息写进代码或提交到 git（SEC 的联系邮箱也放 `.env`：`SEC_EDGAR_USER_AGENT`）。新增密钥：在 `Secrets` 加字段，并在 `.env`、`.env.example` 和 README 表格里各补一行。
- 新增大模型供应商：实现 `llm/base.py` 的 `ChatProvider` → 在 `llm/client.py` 的 `build_provider` 注册；OpenAI 兼容的服务只需在 settings.yaml 加一个 `openai_compat` 档案。
- 新增事件来源：实现 `sources/base.py` 的 `EventSource.collect` → 在 `sources/__init__.py` 的 `build_event_sources` 注册；财报事件的 key 必须用 `models.earnings_key`。
  宏观日程放 `sources/events_macro.py`：继承 `_CachedSource`（下载失败沿用上次结果），用 `macro_event()` 生成事件（kind = macro，symbol = 分组代码），
  key 要能跨改期保持不变（按数据所属期间 / 当年第几次会议，不按发布日期），来源名加进 `models.EVENT_SOURCE_RANK` 和飞书日历的 `SOURCE_NAMES`。
- 注释和推送文案用中文，标识符用英文。

## 常用命令

```bash
tsen check                 # 检查配置、密钥与各数据源
tsen replay --days 5       # 回放最近 5 个交易日，评估阈值
tsen brief premarket       # 生成一份盘前简报，只打印（加 --send 才推送）
tsen events                # 刷新事件日历并生成汇总，只打印（加 --send 才推送）
tsen tick                  # 立即检测一次（各资产按自己的交易时段）
tsen assets                # 盯盘资产与警报标准，换算成几倍 σ
tsen scope                 # 监控范围：持仓 × 主题（基准、同业、夜间信号、相关公司、检索词、板块指标）
tsen scope add LITE        # 给新持仓找联动、出方案、回放预览，确认后写入（suggest 只出方案；remove；undo；check 联动体检）
tsen scope prompt LITE     # 给 Claude Code 准备联动候选的上下文（项目技能 add-holding；写到 data/scope_candidates/LITE.json）
tsen scope tune MU         # 调警报阈值：按日波动建议或 --day 5,8,12 --rapid 60:3，回放对比改前改后，确认后写入
tsen macro                 # 宏观快照
tsen ask "MU 今天为什么跌？"  # 在终端里测试对话（带工具）；加 --keep 用正式库并记住上下文（/new 清空）
tsen feishu setup          # 飞书：建立长连接，打印发消息会话的 chat_id
tsen feishu test           # 飞书：向 FEISHU_CHAT_ID 发测试消息与回复
tsen feishu calendar-setup # 建飞书日历、共享给用户并同步已有事件（可重复运行）；找不到 open_id 时加 --open-id ou_xxx
tsen feishu bitable-setup  # 建异动多维表格、共享给用户，并写入已有异动
tsen feishu sync           # 立即同步日历 / 表格，并拉回 label / notes
tsen run                   # 常驻运行
pytest -q                  # 跑测试
```
