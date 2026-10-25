# tickersentinel

> **English summary.** A personal market-watch assistant built around *your own* holdings: scheduled briefs, round-the-clock
> detection of sharp intraday moves (pre-market, regular, after-hours, plus overseas proxies overnight), automatic attribution of
> each move from news / filings / the event calendar (local LLM by default), an earnings-and-macro event calendar, and push +
> two-way chat via Feishu (Lark). Configuration is per holding and per sector "theme"; nothing is hard-coded to one industry.
> The docs and messages are in Chinese.

按你自己的持仓 / 关注列表定制的盯盘与投研小助理。平时不出声，只在需要你知道的时候推送：

- **定时简报**：盘前 07:30、收盘后 15 分钟、周六周报（时间都可以改）
- **全天候异动监控**：每分钟检测一次，**只有出现急涨急跌才推送**，随后自动检索新闻并给出可能原因；每条附一段宏观快照
  （美债利率、原油、金银、美股大盘……）。同一板块的几只一起动时合成一条推送、归因一次，10 分钟内跟着动的挂在第一条下面作回复
  （`monitor.merge_minutes`，0 = 每只单独推）。美股盯盘前 + 盘中 + 盘后（美东 04:00–20:00），夜里由首尔 / 东京的同一家公司或同业接力，
  黄金白银等期货几乎 24 小时。持仓和每个资产多灵敏在 `config/assets.yaml`；每个板块的联动（板块基准、同业、夜间信号、相关公司、
  新闻检索词、板块指标）是 `config/themes/` 里的一个“主题”文件，同一板块的持仓共用，增删持仓时联动范围自动跟着变
- **事件日历**：提前收集未来 100 天的日程，每晚 20:00 汇总推送（只在有明天的事件或新增 / 改期时推送；
  估计日期只报 45 天以内的变化，远期估计改来改去不打扰，已确认的不限），并写进简报；异动归因时也会对照异动前后的日程。
  - 公司：持仓及相关公司（云厂商、AI 芯片与代工、存储同业、设备、终端）的财报日，持仓的内部人拟减持（Form 144）、锁定期到期（手工录入）
  - 宏观：美联储议息 / 纪要 / 主席讲话，美国 CPI、PPI、非农、JOLTS、PCE、GDP、零售销售的公布时间，长端国债拍卖，
    日本央行议息，韩国出口，美股期权到期与休市（见下文「事件日历收集什么」）
- **大模型**：默认用本机 LM Studio 里的 Qwen 27B（不花钱、数据不出本机）；也可以按任务切换到 Claude 或 OpenAI
- **推送与对话**：飞书机器人推送简报、异动、归因和事件汇总；给它发消息就能提问，它会自己查行情、新闻、异动记录和事件日历再回答。
  没配置飞书时只打印到终端；Telegram 尚未实现
- **飞书日历与多维表格**（可选，默认关闭）：事件日历同步成飞书日程；每条异动 + 归因写进多维表格，事后在手机上标注真实原因

仓库里的 `config/` 是通用示例：持仓示例是一只光模块股（Lumentum，LITE），主题库里有「光模块」和一个备用的「存储芯片」
（首尔 / 东京的夜间信号都写好了）。配置分三处：`assets.yaml`（持仓、检索词、警报灵敏度）→ `themes/`（每个板块一个文件：基准、
同业、夜间信号、相关公司、检索词、板块指标）→ `settings.yaml`（与具体股票无关的设置：时区、基础宏观变量 `macro`、宏观日程 `events.macro` 等）。
`tsen scope` 一屏看完当前的监控范围。

**用自己的持仓**：把 `config/` 整个复制成 `config.local/`（已在 `.gitignore` 里，不会提交），在 `.env` 里加一行
`TICKERSENTINEL_SETTINGS=config.local/settings.yaml`，以后只改那一份（`tsen scope add / tune` 也写到那里）。这样持仓不会随 git 公开，
拉取仓库更新时也不会和示例配置冲突。

```bash
cp -R config config.local
echo "TICKERSENTINEL_SETTINGS=config.local/settings.yaml" >> .env
```

设计细节和开发进度见 [CLAUDE.md](CLAUDE.md)。

> **免责声明 / Disclaimer**：本项目仅供个人学习与研究，**不构成任何投资建议**。行情来自免费或非官方的数据源（如 Yahoo Finance），
> 可能延迟、缺失或出错；异动归因由大模型根据检索到的新闻生成，可能不准确。据此做出的任何交易决定由使用者自行负责。
> 使用时请遵守各数据源（Yahoo、SEC EDGAR、飞书开放平台等）的服务条款和访问频率要求。
> *For personal research only — not investment advice. Market data may be delayed or wrong; LLM-generated explanations may be
> inaccurate. Use at your own risk and respect each data provider's terms of service.*

**快速开始**（不需要任何 key：行情、新闻、公告、宏观日程都有免费来源）：

```bash
git clone https://github.com/fengchaoya/tickersentinel.git && cd tickersentinel
uv venv --python 3.12 && uv pip install -e ".[dev]"
cp .env.example .env               # 先不填也能跑；SEC 公告建议填一个联系邮箱
.venv/bin/tsen check               # 检查配置和各数据源
.venv/bin/tsen replay --days 5     # 用最近 5 个交易日的真实分钟数据回放，看看会推送什么
```

先把 `config/settings.yaml` 里的 `timezone` 改成你自己的时区（IANA 名称，如 `America/New_York`）。定时简报的日程规划按本地日期安排，
目前按美洲时区设计（亚洲 / 欧洲时区下盘前、收盘简报的时间点可能需要自己调整）。本地大模型可选（LM Studio，约 16 GB 内存）；
没有大模型时简报、归因自动降级为新闻列表。

## 1. 安装

推荐用 [uv](https://docs.astral.sh/uv/)（会自动下载合适的 Python 版本）：

```bash
brew install uv                       # 或：curl -LsSf https://astral.sh/uv/install.sh | sh
git clone https://github.com/fengchaoya/tickersentinel.git
cd tickersentinel
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev,feishu]"         # 用飞书推送 / 对话 / 同步时需要 feishu 这一组
```

装好以后，`tsen` 命令在 `.venv/bin/` 里。怎么让终端找到它见「3. 运行」的第一小节。

不用 uv 的话（需要 Python ≥ 3.10）：

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## 2. 配置

1. **密钥与个人信息**放在项目根目录的 `.env`（已在 `.gitignore` 中，不会提交）。文件里列好了全部变量，
   等号后直接填值即可（新克隆的仓库先运行 `cp .env.example .env`）。**不填也能运行**，对应功能会自动降级或跳过：

   | 变量 | 用途 | 申请地址 | 不填会怎样 |
   |---|---|---|---|
   | `SEC_EDGAR_USER_AGENT` | SEC 公告请求头里的联系方式，如 `tickersentinel 你的邮箱` | 不用申请，**填自己的邮箱** | 用占位邮箱，SEC 可能限流或拒绝 |
   | `BLS_USER_AGENT` | 美国劳工统计局（BLS）发布日程的请求头联系方式，格式同上（可以和 SEC 那行一样） | 不用申请 | 事件日历里没有 CPI、PPI、非农、JOLTS 的公布时间（BLS 拒绝不带联系邮箱的请求） |
   | `ANTHROPIC_API_KEY` | 云端大模型 Claude（可联网检索），要在 `llm.tasks` 里选用才会调用 | console.anthropic.com | 只用本地模型 |
   | `OPENAI_API_KEY` | 云端大模型 OpenAI | platform.openai.com | 只用本地模型 |
   | `FINNHUB_API_KEY` | 公司新闻、财报日历 | finnhub.io（免费） | 少一个新闻源；财报日仍可从 yfinance 获取 |
   | `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` | 行情兜底（Yahoo 出错时接上）、日线官方收盘价 | alpaca.markets（免费，不用装 alpaca-py） | 只用 yfinance、不设兜底 |
   | `FEISHU_*` / `TELEGRAM_*` | 推送与对话（飞书可用；飞书应用也用于日历 / 多维表格同步；Telegram 尚未实现） | 飞书：open.feishu.cn（步骤见下文「推送：飞书」） | 只打印到终端（缺密钥时警告并自动退回）；不能在手机上对话；飞书同步不生效 |
   | `OPENDART_API_KEY` | 韩国 DART 公告（尚未实现） | opendart.fss.or.kr | — |

   `.env.example` 是提交到 git 的空白模板，只列变量名，不放任何真实值。

2. 打开 `config/settings.yaml`（可选）：时区、简报时间、检测频率与冷却、合并推送、事件日历、基础宏观变量、大模型、推送渠道
   `notify.provider`。持仓、公司检索词、警报阈值在 `config/assets.yaml`，板块联动和板块检索词在 `config/themes/`。
   示例配置的推送渠道是 `console`（只打印到终端）；接好飞书后改成 `feishu`。选了 `feishu` 但没填密钥或没装 SDK 时只在日志里警告一句、
   退回终端打印，不会启动就崩溃。
   `telegram` 还没实现，先不要选（填了密钥又选它，每次推送都会报错）。

### 本地大模型（LM Studio）

默认所有大模型任务（简报、异动归因、事件汇总、对话）都用本机 LM Studio 里的 `qwen/qwen3.8-27b`（4bit，约 16 GB 内存）：

- 需要先装好 [LM Studio](https://lmstudio.ai) 并下载该模型（`lms ls` 能看到即可）。**不用手动启动服务**：程序第一次调用时会自己执行
  `lms server start` 和 `lms load`（上下文 32768），闲置 60 分钟后 LM Studio 会自动卸载模型释放内存。
- 已经关闭“思考”模式（`reasoning_effort: none`）：整理新闻不需要长推理，速度快数倍。本机实测一份简报约 40 秒、一次归因约 15 秒、一次对话 20–50 秒。
- 本地模型不能联网检索，归因只依据程序收集到的新闻和公告。需要联网补充检索时，把 `llm.tasks.attribution` 改成
  `[anthropic]`（只用 Claude）或 `[local, anthropic]`（本地失败时才用 Claude），并在 `.env` 里填 `ANTHROPIC_API_KEY`。
- 换模型：改 `llm.providers.local.model`；换成 Ollama 等其他本地服务：改 `base_url`，并删掉 `lmstudio` 那几行。

### 推送：飞书

推送和回复都走飞书机器人，用长连接收消息，**不需要公网地址或服务器**。一次性设置，约 15 分钟：

0. **安装 SDK**：`uv pip install -e ".[feishu]"`。用的是国际版 Lark 的话，把 `settings.yaml` 的 `notify.feishu_domain` 改成 `lark`。
1. **创建应用**：打开 [飞书开放平台开发者后台](https://open.feishu.cn/app)（Lark：open.larksuite.com/app）→「创建企业自建应用」，
   名称如 `tickersentinel`。需要你在一个有管理员权限的飞书企业 / 团队里；没有的话可以在飞书客户端里免费新建一个团队。
2. **开通机器人**：应用详情 →「添加应用能力」→「机器人」。
3. **拿到凭证**：「凭证与基础信息」页的 App ID、App Secret，写进 `.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。
4. **开通权限**：「权限管理」里搜索并开通
   `im:message`（获取与发送单聊、群组消息）、`im:message:send_as_bot`（以应用的身份发消息）、
   `im:message.p2p_msg:readonly`（读取用户发给机器人的单聊消息）、`im:message.group_at_msg:readonly`（接收群聊中 @ 机器人的消息）。
5. **建立长连接**：在终端运行 `tsen feishu setup`，看到“已连接飞书”后**保持它运行**。
6. **订阅事件**：「事件与回调」→「事件配置」→ 订阅方式选「使用长连接接收事件」并保存（后台会检测第 5 步的连接），
   再「添加事件」→ `im.message.receive_v1`（接收消息）。
7. **发布**：「版本管理与发布」→ 创建版本 → 可用范围选你自己 → 申请发布（你是管理员时直接通过）。以后改权限都要重新发布一个版本。
8. **查 chat_id**：在飞书里搜索这个机器人，给它发一句话（想发到群里就把它拉进群并 @ 它）。第 5 步的终端会打印
   `FEISHU_CHAT_ID=oc_...`，机器人也会回复这个值。把它写进 `.env`，然后按 Ctrl+C 结束 setup。
   终端里同时打印的“发送人 open_id”（`ou_...`）是你自己的飞书 ID：用群聊的话，后面设置飞书日历 / 多维表格时要用到。
9. **验证**：`tsen feishu test` 会发一条测试消息和一条挂在它下面的回复；`tsen check` 会显示机器人名称。
10. **切换渠道**：`settings.yaml` 里 `notify.provider: feishu`。之后简报、异动、归因、事件汇总都推到飞书，
    其他会话的消息一律忽略。启动消息里的“推送渠道”一行是实际在用的渠道（缺密钥退回终端时会注明）。

### 和机器人对话

推送渠道是飞书、常驻运行（`tsen run`）时，在 `FEISHU_CHAT_ID` 这个会话里给机器人发消息（群里要 @ 它），它先回“收到，思考中…”（`chat.ack`），
再用大模型（默认本机模型，`llm.tasks.chat`）回答，**约 20–50 秒**（要先调用查询工具再作答，一般调用模型 2–3 次）。
凡是涉及最新情况的问题，模型都会先调用下面 5 个只读工具，数字和日期只用工具查到的，查不到就直说：

| 工具 | 查什么 | 可以这样问 |
|---|---|---|
| `get_macro` | 宏观变量（见下面「宏观数据」）：美债 10 年 / 30 年收益率与官方 2 年期、2s10s 利差，WTI 原油，黄金、白银，标普 500、纳斯达克（休市时为股指期货），VIX，美元指数，美元/日元，比特币 | “现在长端利率和油价怎么样”“今天是宏观在带节奏吗” |
| `get_intraday` | 今天（休市时为最近一个交易日，收盘后用官方收盘价）的走势：较昨收、最近 N 分钟的涨跌，各自拆成板块部分（β × 基准）和个股特异部分，附同业同期涨跌 | “SNDK 为什么跌”“是板块带的还是个股的”“这半小时怎么了” |
| `search_news` | 最近 1–168 小时的新闻与 SEC 公告，最多列 10 条，其中给 SEC 公告留 4 个名额（否则常被上百条新闻挤掉）。持仓会自动用预设的中英韩检索词；其他关键词按提问的语言查 Google News | “MU 最近有什么新闻”“这周有没有新的 Form 4”“HBM 价格最近怎么说” |
| `recent_alerts` | 最近 1–30 天推送过的异动、板块 / 个股拆分和当时的归因 | “这周有过异动吗”“上次那次急跌是什么原因” |
| `upcoming_events` | 事件日历：财报日（标明预计 / 已确认）、Form 144 拟减持、解禁，以及宏观日程（议息、经济数据、国债拍卖、休市……）；公司事件带代码并标出是不是你的持仓。可以只看宏观或只看公司。至少看 14 天，最远到日历的收集范围；只问一家公司时直接查到收集范围 | “SKHY 什么时候发财报”“接下来两周有哪些事件”“下次议息 / CPI 是哪天” |

- 代码和名称都认：“美光”“Micron”“海力士”“SK hynix” 都能对到持仓。英文名只按完整单词（至少 4 个字母）匹配，
  所以问 ON、AD 这类短代码时按代码本身查，不会被当成某个持仓名称的一部分。
- 它还知道你的持仓（含 `config/assets.yaml` 里每个资产的 `note` 补充背景）、你“回复”的那条推送、最近 5 轮对话（`chat.history_turns`）。
  发 `/new` 开始新对话；发 `/macro`（或“宏观”）直接回一份宏观快照，不经过大模型，几秒钟。
- 个股的最新价不做成工具（在券商 App 里看更直观）；问“为什么涨跌”时 `get_intraday` 会给出走势和拆分。
- 新闻引用：模型只写编号，程序换成真实链接；模型自己写的网址会被去掉，不会出现编造的链接。
- 对话不联网检索（`llm.tasks.chat` 换成 Claude 也一样），新闻只来自程序配置的新闻源。事件日历只收集未来 100 天（`events.lookahead_days`），更远的日期答不了，
  工具会注明日历收集到哪天，不让模型去猜。
- 它只回答问题：不能设提醒、不能下单、不能“下次再告诉你”；涉及买卖时给分析框架，不给指令。
- **在手机上调监控范围**（`/scope`，不经过对话模型）：
  - `/scope`（或“监控范围”）：看现在的范围；
  - `/scope add 代码 [主题]`：给新持仓找联动，回复方案、范围会怎么变、按建议阈值回放几天大概推多少条，约 1 分钟；
  - 你回「确认」才写入，回「取消」放弃；回「去掉 代码」「加上 代码」可以先调整候选；
  - `/scope remove 代码`：移除持仓；`/scope undo`：撤销最近一次改动；这两项也要回「确认」才执行；
  - `/scope check`：联动体检；
  - `/scope tune 代码 [全天 5 8 12] [急涨 60:3]`：调警报阈值，附改前改后的回放对比，回「确认」写入。急涨急跌只改写出来的窗口
    （其他窗口不变），写 `60:0` 去掉这一档。

  待确认的改动 30 分钟内有效。确认后**立即生效，不用重启**；用 `tsen scope` 或手工改了 `assets.yaml` / `config/themes/`，
  正在运行的 `tsen run` 也会在一分钟内自动读到，并推一条“监控范围已更新”（文件改坏了只提醒一次，继续按原来的范围运行）。
  `settings.yaml` 里其他设置的改动仍要重启。`tsen ask "/scope add LITE"` 可以在终端里预览同样的回复（预览不写配置）。
- 一次提问最多 `chat.max_tool_rounds` 轮工具调用（默认 4，可设 1–10），之后要求直接作答。

**在终端里测试**（不开飞书也行，和飞书里的回答是同一套逻辑与工具）：

- `tsen ask "问题"`：只读预览。把正式数据库复制一份进内存（读得到历史异动；之前用 `--keep` 留下的终端对话也会带上），
  先刷新一次事件日历再回答；问答不保存，不影响正式运行。
- `tsen ask --keep "问题"`：直接用正式数据库，问答记进一个“终端”会话（和飞书里的会话分开），可以连续追问；
  `tsen ask --keep /new` 清空。事件日历用库里现有的（由 `tsen run` 定时刷新）。

### 宏观数据

每条异动推送末尾附一段宏观快照，归因模型也会参考（长端利率、油价、美元、大盘在同一时段明显变化、且板块同向时，
更可能是宏观驱动）。随时查看：飞书里发 `/macro`（或“宏观”），终端里 `tsen macro`。变量在 `settings.yaml` 的 `macro.indicators`
里增删，`in_alert: false` 的只在 `/macro` 里显示。这些是**基础宏观变量**，和具体股票无关；和某个板块有关的指标（例如存储芯片的
美元/韩元、KOSPI）写在那个主题的 `indicators` 里，只附在这个板块的异动推送上，`/macro` 里单独列成“板块指标”。

| 变量 | 数据 | 涨跌口径 |
|---|---|---|
| 美债 10 年 / 30 年 | Cboe 收益率指数 `^TNX` / `^TYX`（数值即收益率 %），约延迟 15 分钟 | 较昨收，单位 bp |
| 美债 2 年、2s10s 利差 | 美国财政部每日收益率曲线（官方，前一交易日收盘） | — |
| WTI 原油、黄金、白银 | CME 连续合约 `CL=F` / `GC=F` / `SI=F`，约延迟 10 分钟 | 较 24 小时前 |
| 标普 500、纳斯达克 | 现货指数 `^GSPC` / `^IXIC`；美股休市时换成期货 `ES=F` / `NQ=F` | 较昨收；期货为较美股收盘 |
| VIX、美元指数、美元/日元、比特币 | `^VIX`、`DX-Y.NYB`、`JPY=X`、`BTC-USD` | VIX 较昨收，其余较 24 小时前 |

另外每项都给“近 1 小时”变化（市场没在交易时不给）。涨跌只用分钟线算，不用日线：期货换月时日线和分钟线是不同合约，
用日线会算出假的大涨大跌（实测原油差 4%）。取宏观数据最多等 10 秒（`macro.timeout_seconds`），取不到就不附，不耽误异动推送；
异动入库时连同当时的宏观快照一起存（`alerts.macro_json`），以后做事件研究可以直接用。

### 事件日历收集什么

都在 `settings.yaml` 的 `events` 里增删：

- **公司**：持仓、它们的海外主上市代码，以及持仓所属主题（`config/themes/`）里的同业 `peers` 和相关公司 `catalysts` 自动收集。
  存储芯片主题列了云厂商（微软、谷歌、亚马逊、Meta、甲骨文）、AI 芯片与代工（英伟达、AMD、博通、台积电）、希捷、
  半导体设备（泛林、阿斯麦）和苹果。和具体主题无关、但也想收集财报的公司写在 `settings.yaml` 的 `events.related`。
  卖掉一个主题的最后一只持仓后，它的同业和相关公司的事件也不再出现。
- **宏观**（`events.macro`，都来自官方日程，不需要 key）：

  | 配置项 | 内容 | 来源 |
  |---|---|---|
  | `fed` | `fomc` 议息决议 + 记者会（标注带点阵图的会议）、`minutes` 会议纪要、`chair` 主席讲话与国会证词；可加 `beige_book` 褐皮书 | 美联储官网的议息日程与日历 |
  | `us_data` | `cpi` `ppi` `jobs`（非农）`jolts`：BLS，需要 `.env` 的 `BLS_USER_AGENT`；`pce` `gdp`：BEA；`retail_sales`：人口普查局；可加 `eci` 就业成本指数、`durable_goods` 耐用品订单 | 各机构的发布日程 |
  | `treasury_auctions` | 长端国债拍卖，默认 `[10Y, 20Y, 30Y]`；结果 13:00 美东公布，常牵动长端利率 | 财政部（约提前一周公告） |
  | `boj` | 日本央行议息（日元急升常伴随全球去杠杆） | 日本央行官网 |
  | `korea_exports` | 韩国月度进出口（每月 1 日）与前 20 天出口（21 日） | 按惯例推算，标为“预计” |
  | `opex` | 美股月度期权到期（季月为“四巫日”） | 按交易所日历推算 |
  | `us_market` | 美股休市与提前收盘 | 交易所日历 |

  每晚汇总里宏观和公司分开列；宏观日程提前很久就排好了，所以“新增”不单独提醒，只提醒改期（例如政府停摆推迟数据发布）。
  异动归因时，模型会看到异动前 24 小时内发生的日程（例如盘前 7:30 刚公布 CPI、昨晚同业发了财报）和接下来 24 小时的重大日程。

### 飞书日历与多维表格（可选，默认关闭）

- **事件日历 → 飞书日历**：财报日、Form 144 拟减持、解禁等同步成飞书日程（有精确时间的是 30 分钟的日程，只有日期的是全天日程；
  标题注明“预计”或“盘前 / 盘后”，详情里有来源和链接）。改期、确认会更新同一个日程，过时的估计会被删掉（之后又出现会重新建）；
  7 天以前的日程留在日历里，不再更新。
- **异动 → 飞书多维表格**：每条异动 + 归因一行（时间、标的、规则、较昨收、板块部分、个股特异、归因结论、驱动、原因明细……）。
  `label 真实原因`、`notes 备注` 两列留给你事后填写，程序会把它们拉回数据库的 `alerts.label / notes`，用来评估归因准确率。

两者都由应用（机器人）以自己的身份创建，再共享给你：日历你是只读成员（日历是事件日历的镜像，在飞书里改了也会被覆盖；
要更正日期写进 `events.manual`），多维表格你有完整权限。只需要 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，和推送渠道是不是飞书无关。

> 这部分目前只用模拟的飞书接口做过离线测试，还没有在真实飞书上建过日历和表格；第一次设置时请留意命令的输出。

1. **开通权限**：开发者后台 →「权限管理」→ 开通 `calendar:calendar`（日历及日程的读写）和
   `bitable:app`（查看、评论、编辑和管理多维表格；给你加协作者也靠它），然后「版本管理与发布」**发布一个新版本**
   （不发布不生效）。找你的 open_id 用的是推送那一步已经开通的 `im:message.p2p_msg:readonly`（读单聊里你发过的消息），不用另加。
2. **建日历**：`tsen feishu calendar-setup`。它会找出你的 open_id、建好日历「tickersentinel · 事件日历」并把你加为成员、同步一次。
   然后在飞书「日历」里订阅它：日历列表旁的「+」→「订阅日历」→ 搜索日历名（应用没法替你订阅）。
   不想订阅：把 `notify.feishu_sync.invite_user` 改成 `true`，日程会以邀请的形式直接出现在你的主日历里（不发通知）。
   注意：之后再改回 `false`，**已经加进你主日历的日程不会自动移除**（已知限制），要在飞书里自己处理。
3. **建表格**：`tsen feishu bitable-setup`。建好多维表格「tickersentinel · 异动记录」，打印链接，并写入已有的全部异动。
   之后在飞书「云文档 → 与我共享」里也能找到。
4. **打开开关并重启**：`settings.yaml` 里 `notify.feishu_sync.calendar: true`、`bitable: true`，然后重启 `tsen run`
   （启动消息里的“飞书同步”一行会显示是否真的生效；`tsen check` 只看开关和密钥，权限要用 `tsen feishu sync` 试）。
   常驻运行时：每次刷新事件日历后同步日历；每次盘中检测后在后台把新异动、刚完成的归因写进表格；
   每 15 分钟对一遍账，每小时拉回一次你填的标注。
5. **随时手动同步**：`tsen feishu sync`（同步日历、补齐表格、拉回标注）。

**你的 open_id**（日历成员、表格协作者都按它共享）：两个 setup 命令（以及打开开关后的 `tsen run`）会从 `FEISHU_CHAT_ID` 会话里自动找出来，
但只在能确定是你的时候才用——**会话是群聊时（最近有多人发过言，或者读得到成员列表、有多个成员）不会去猜**，否则可能把表格的编辑权限共享给别人。
这时（或者还没给机器人发过消息时）用 `--open-id ou_xxx` 指定，值就是 `tsen feishu setup` 打印的“发送人 open_id”；指定一次会记住。

**标注列的规则**：

- 程序只更新它自己生成的列；「label 真实原因」「notes 备注」只在插入新行时写一次（库里已有的标注），之后以你在飞书里填的为准。
- 飞书里的**空单元格不会覆盖库里已有的标注**：在飞书里清空不会清掉数据库。真要清空，先在飞书里清空，再在库里清
  （`alerts` 表的 `label` / `notes` 列）。这样做是为了防止列被改名或删掉后，程序补建的空列把辛苦标注的内容全部冲掉。
- 不要改列名：程序按列名读写，改了名会补建一个原名的空列，改过名的那列不再拉回。
- 你在飞书里删掉的行不会再被写回（刚写入 10 分钟内的行除外：飞书检索可能有延迟，过一会儿才会判定）。

**出错时**：缺权限时这几个命令会先列出建议开通的权限（`calendar:calendar` / `bitable:app`）和开发者后台的直达链接。
常驻运行时同步失败不影响推送和盯盘：失败的那部分（日历 / 表格）30 分钟内不再尝试；**每一段连续失败只推送一次提醒，同步成功后重新计**；
单条记录写入失败（例如有人把「时间」列改成了文本）也算失败。打开了开关但缺密钥或没装 SDK 时不会崩溃，只在日志里警告。

`tsen feishu sync` 和 `tsen run` 共用正式数据库，偶尔手动跑一次没问题；但**不要让它和 `tsen run` 长时间同时运行**
（比如用 cron 反复执行）：两个进程各自加锁、互相看不见，同时写同一张表会撞上飞书的并发写冲突。`tsen run` 本身已经会自动同步；
按 Ctrl+C 退出时会先等正在进行的同步写完（最多 30 秒，没写完的下次启动再对账）。

其他可选项（日历 / 表格名称、你在日历上的角色、有具体时间的日程显示多长、同步多久以前的事件）见 `src/tickersentinel/config.py` 的 `FeishuSyncCfg`。

## 3. 运行

### 先让终端找得到 `tsen`

`tsen` 装在项目自己的虚拟环境 `.venv` 里，**新开的终端默认找不到它**，直接敲会报 `zsh: command not found: tsen`。
三种办法任选一种：

```bash
# 办法一（推荐，一次设置、以后任何终端都能用）：把它链接到已经在 PATH 里的目录
mkdir -p ~/.local/bin && ln -sf "$PWD/.venv/bin/tsen" ~/.local/bin/tsen   # 在项目目录里执行
echo $PATH | tr ':' '\n' | grep -x "$HOME/.local/bin" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

# 办法二：每次新开终端时先激活虚拟环境（提示符前面会出现 (.venv)）
cd /path/to/tickersentinel && source .venv/bin/activate

# 办法三：不激活，写全路径
.venv/bin/tsen check
```

确认：`which tsen` 能打印出路径、`tsen --help` 能列出命令就好了。办法一的链接指向项目里的 `.venv`，
所以移动或改名项目目录后要重新链接一次（`.venv` 本身也要按「1. 安装」重建）。

`tsen` 是 `tickersentinel` 的简写，也可以写成 `python -m tickersentinel`（同样要在虚拟环境里）。
在任何目录下运行都可以：配置文件（`config/settings.yaml`、`.env`）和数据库按项目目录找，不按当前目录。

### 常用命令

```bash
tsen check                  # 先检查：密钥、各数据源、大模型、推送渠道能否连通，飞书同步开没开
tsen macro                  # 宏观快照：美债利率、原油、金银、美股大盘、VIX、美元……
tsen replay --days 5        # 用最近 5 个交易日的真实分钟数据回放，看当前阈值会推送多少条
tsen brief premarket        # 立即生成一份盘前简报（只打印；加 --send 才走推送渠道）
tsen events                 # 刷新事件日历并生成一份汇总（只打印；加 --send 才推送）
tsen tick                   # 立即检测一次（各资产按自己的交易时段）
tsen assets                 # 盯盘资产与警报标准，和最近的波动对比（调灵敏度时参考）
tsen scope                  # 监控范围：持仓 × 主题（基准、同业、夜间信号、相关公司、检索词、板块指标）
tsen scope add LITE         # 新持仓：自动找联动、出方案（并入已有主题或新建），回放预览，确认后写入
tsen scope suggest LITE     # 只出方案，不写入（add 加 --drop A,B / --keep C 调整候选；不是交互终端时只预览，加 --yes 才写入）
tsen scope prompt LITE      # 打印在 Claude Code 里准备联动候选需要的上下文和格式（项目技能 add-holding 会用到）
tsen scope remove LITE      # 移除持仓（主题文件保留备用）
tsen scope undo             # 撤销最近一次改动
tsen scope check            # 联动体检：同业、夜间信号还联动吗，有没有新的强联动候选
tsen scope tune MU          # 调警报阈值：按日波动建议（或 --day 5,8,12 --rapid 60:3），回放对比改前改后的推送量，确认后写入
tsen ask "问题"             # 在终端里向机器人提问（与飞书里的对话同一套逻辑和工具；只读预览，不留记录）
tsen ask --keep "问题"      # 同上，但记住问答、可以连续追问（tsen ask --keep /new 清空）
tsen feishu setup           # 飞书：建立长连接，打印会话的 chat_id 和你的 open_id
tsen feishu test            # 飞书：发一条测试消息和一条挂在它下面的回复
tsen feishu calendar-setup  # 飞书：建事件日历并共享给你（群聊时加 --open-id ou_xxx）
tsen feishu bitable-setup   # 飞书：建异动多维表格并共享给你（群聊时加 --open-id ou_xxx）
tsen feishu sync            # 飞书：立即同步日历、补齐表格、拉回你填的标注
tsen run                    # 常驻运行：定时简报 + 全天候异动监控 + 飞书对话（Ctrl+C 退出）
```

实际推送渠道是终端时（包括飞书缺密钥退回终端），推送内容同时会写入 `data/outbox.log`；飞书推送和不加 `--send` 的预览不写。异动和归因记录保存在
`data/tickersentinel.sqlite3` 的 `alerts` 表，事件日历在 `events` 表。

**手工录入事件**（锁定期到期、股东大会等）：写在 `settings.yaml` 的 `events.manual` 里，例如
`- {symbol: SKHY, day: 2027-01-05, kind: lockup_expiry, title: "SK海力士 ADR 承销锁定期到期"}`。

### 盯哪些资产、多灵敏：`config/assets.yaml`

一个文件管两件事：**纳入哪些资产**（持仓，以及只盯盘、不算持仓的资产），**每个资产的警报标准**。文件开头有逐项说明，大意：

```yaml
defaults:                       # 每类资产的默认标准；单个资产可以在自己的 alert 里覆盖
  us_equity: {hours: extended, rapid_pct: {5: 1.5, 15: 2.5, 30: 3, 60: 3}, z: 4.0, day_pct: [5, 8, 12]}
  future:    {rapid_pct: {5: 0.6, 15: 1.0, 30: 1.4, 60: 1.4}, z: 4.0, day_pct: [1.5, 2.5, 4]}   # 黄金白银波动小得多
assets:
  - {symbol: MU, name: 美光 Micron, class: us_equity, theme: memory, queries: {en: ["Micron Technology"]}}
  # - {symbol: "GC=F", name: 黄金, class: future, alert: {day_pct: [1.2, 2, 3]}}           # 开始持有黄金时去掉 #
```

两条规则，都是“从起点到现在走了几个点”（按参照价的百分点算：“+2% 跌到 -1%”是 3 个点）：

- **急涨急跌** `rapid_pct`：{窗口分钟: 下限%}。最近这么多分钟里的**最高点**到现价跌了这么多（或**最低点**到现价涨了这么多），
  同时 z 值 ≥ `z`（相对它自己近 5 天的波动足够反常，高波动的日子自动收紧）。看高低点而不是窗口起点，所以“先冲高、再跌回水下”
  这种冲高回落能抓到；60 分钟一档管用了半小时到一小时的回落。数字越小越灵敏。
- **全天累计** `day_pct`：从“昨收和今天最高点里更高的那个”算起跌了这么多（涨的方向对称：昨收和最低点里更低的那个），
  每一档推一次，创出新的高 / 低点后重新计。管开盘跳空、几个小时的一路阴跌、慢速的冲高回落 / 探底回升。
  参照价：美股盘前 / 盘中 = 昨收（盘中会接着盘前的高低点算），**盘后 = 今天收盘**；期货 = 上一交易时段结束（美东 17:00）；
  海外股票 = 当地昨收。（2026-10-24 以前是“较昨收突破 3/5/8/12% 关口”，旧名 `levels_pct` 还能读，按新规则解释。）
- 同一波行情不重复推：同方向 15 分钟（或窗口长度）内只推一次，幅度扩大到 2 倍才再推一条“扩大”；急涨急跌刚推过的，全天的那一档不再单独推。
- 同一主题的几只同方向一起异动：同一分钟里的合成一条（“存储芯片 3 只一起跌：MU、SNDK、SKHY”，归因一次）；之后 10 分钟内
  同主题同方向又触发的，挂在第一条下面作回复，不再附宏观快照，个股因素为主的照样单独归因。异动本身仍然一条条入库。
  10-09～10-23 回放：102 条异动 → 98 条推送，其中 19 条是挂在前一条下面的回复。
- **时段** `hours`（只对美股）：`extended` = 盘前 04:00 + 盘中 + 盘后到 20:00（美东）；`regular` = 只盯常规时段。
- 资产类别 `class`：`us_equity` 美股 / ADR、`foreign_equity` 海外股票（按当地交易所时段）、`future` 期货（CME，美东 18:00–次日 17:00）、`crypto`。

### 板块联动：`config/themes/`

“加一只股票”简单，难的是和它联动的那一圈：板块基准、同涨同跌的同业、美股休市时能预示它的海外股票、会牵动它的大客户财报……
这些按板块写成“主题”，一个文件一个（文件名就是主题名），同一板块的持仓共用。持仓在 `assets.yaml` 里用 `theme:` 指向它；
**没有持仓引用的主题不生效**（不取数据、不收集事件、不推送），所以主题文件可以先放着备用。

```yaml
# config/themes/optics.yaml（仓库里的备用模块）
name: 光模块
focus: 数据中心光模块与光器件（800G / 1.6T、CPO、硅光）     # 写进提示词
benchmark: basket                 # 板块基准：ETF / 指数代码，或 basket（下面 peers 里美股的等权篮子）
peers: {COHR: Coherent, AAOI: Applied Optoelectronics, VIAV: Viavi, CIEN: Ciena, GLW: 康宁}   # 同涨同跌
overnight: {"300308.SZ": 中际旭创, "300502.SZ": 新易盛}       # 夜间信号：自动成为只盯盘的资产
catalysts: {NVDA: 英伟达, ANET: Arista, MSFT: 微软}          # 大客户 / 供应商：只进事件日历和归因
news: {en: ["optical transceiver"], zh: ["光模块"]}          # 板块新闻检索词
indicators:                       # 板块指标：只附在这个板块的异动推送上
  - {key: cny, name: 美元/人民币, symbol: "CNY=X", kind: level, ref: 24h}
```

以后买 LITE，只要在 `assets.yaml` 加一行 `- {symbol: LITE, name: Lumentum, theme: optics}`：
- 拆分“板块 / 个股”时，LITE 用光模块篮子，MU 仍用 SOXX，互不干扰。实测 LITE 对 SOXX 的 R² 只有 0.40，对篮子 0.71；
- 推送里的“同期”只列光模块同业；
- 夜里盯中际旭创、新易盛；
- 事件日历多收集 COHR、Arista 等的财报；
- 新闻只检索光模块的板块词。

不想手写主题文件的话，`tsen scope add 代码` 会替你找：候选来自 Yahoo 的“相似股票”和行业龙头、Finnhub（有 key 时）、已有主题，
以及“大模型提名”的同业、海外对应公司、大客户和检索词。提名要靠模型自己的知识，本地 27B 模型不够用，默认交给 **Claude Code**
（用订阅额度，不调用 API、不花额外的钱）：在本项目里打开 Claude Code，说“给 LITE 准备联动候选”或“加一只持仓 LITE”，
项目技能 `add-holding`（`.claude/skills/`）会联网核实代码，写好 `data/scope_candidates/LITE.json`，再替你运行 `tsen scope add` 出方案，
你确认后才写入。这份候选 30 天内有效，飞书里发 `/scope add LITE` 也会用上。没准备候选时跳过这一步，只用 Yahoo / Finnhub / 已有主题的候选。
想让程序自己调用线上 Claude，就把 `llm.tasks.scope` 写成 `[claude_research]` 并填 `ANTHROPIC_API_KEY`（每次约 0.2–0.5 美元）。
所有候选都再用近一年日线逐个打分。具体规则：
- 同业：剔除大盘 SPY 后的残差相关 ≥ 0.5；
- 板块基准：在行业 ETF 和同业篮子里挑 R² 高的；
- 夜间信号：海外股票当天涨跌和它次日开盘跳空的偏相关 ≥ 0.3；
- 警报标准：按它自己的日波动换算（默认值按日波动 5% 的股票定，σ 是 2.5% 的就减半）。
- 夜间信号：同一交易所有两只以上时合成一个等权篮子触发（见下面）。

和已有主题联动够强（平均 ≥ 0.6）就直接并进去，不新建。方案里列出每个数字、样本天数、候选来源和没选上的候选，
大模型给的名称和 Yahoo 登记的名称对不上时会并排显示，代码记错了一眼能看出来。写入前还会按建议的阈值回放几天，告诉你大概会推多少条。
写入会先备份（`data/scope_history/`），`tsen scope undo` 撤销。
实测：`tsen scope suggest LITE` 并入光模块主题（平均 0.75），约 40 秒；`tsen scope suggest AMD` 新建“算力 / 计算芯片”主题，
基准 SOXX（R² 0.78，高于篮子），同业 INTC 0.71、MU 0.61……，NVDA 只有 0.14，放进相关公司。

卖掉 LITE，这一整圈自动停掉。四种联动不能混在一起：英伟达是光模块最大的客户，但它和 LITE 剔除大盘后的日收益相关只有 0.29，
放进 `peers` 会把“板块联动”的拆分带偏，所以放在 `catalysts`。`tsen scope` 查看现在生效的范围。

**夜间信号按篮子触发**：同一主题在同一交易所有两只以上夜间信号时（存储芯片的首尔：SK海力士 + 三星电子，日收益相关 0.87），
合成一个等权篮子来检测，一波行情只推一条“存储芯片夜间信号（首尔篮子）”，正文列出各成分的涨跌。
某一只自己涨跌 ≥ 3% 且相对篮子也偏离这么多时，多半是这家公司自己的消息，才单独推（`overnight_single_pct`）。
- 实测：最近 10 个交易日，首尔的夜间推送从 49 条降到 24 条；
- 篮子的阈值写在主题的 `overnight_alert`；
- 想每只单独推，就设 `overnight_basket: false`；
- 东京的铠侠（和闪迪合资经营闪存工厂）和首尔同一时段交易，但东京只有它一只，所以单独触发。它的日波动约 6%，
  阈值单独写在它的 `alert` 里（急涨急跌 1.9/3/3.5/3.5%、全天 6/10/15%）。东京、上海深圳、香港有午休：午休前后的价差不算
  “几分钟内急涨急跌”（复盘后先预热 10 分钟，窗口不跨午休），跳空够大时由全天累计推。

**调灵敏度的方法**：
1. `tsen assets`：列出每个资产的阈值，换算成“相当于它最近几倍的波动（σ）”。例如美光 5 分钟 1.5% ≈ 7σ，而黄金 5 分钟的 σ 只有约 0.05%，
   套用个股的 1.5% 就是 30σ、几乎永远不会触发——所以不同资产要用不同的数。
2. `tsen replay --days 10`：按当前阈值回放最近 10 个交易日（含盘前、盘后、海外时段），看会推送多少条、都在什么时候。太多就调大，太少就调小，再回放。
3. `tsen scope tune 代码`（飞书 `/scope tune 代码`）：按这只资产最近 120 个交易日的日波动给建议阈值（类别默认值 × 日波动 / 这组默认值针对的日波动：股票 5%、期货 2%），
   也可以直接给数字：`--day 5,8,12 --rapid 60:3`，飞书里写“全天 5 8 12 急涨 60:3”（急涨急跌只改写出来的窗口，`60:0` 去掉这一档）。
   它会把最近 10 个交易日按“原来 / 改后”各回放一遍，
   告诉你推送量怎么变，确认后写入，可以撤销。新增持仓时 `tsen scope add` 也按同样的规则定初始阈值。这是纯计算，不需要大模型。
   代码会自动写到该写的地方：持仓写进 `assets.yaml`，夜间篮子写进主题的 `overnight_alert`；篮子可以用主题名或任何一个成分指定。
   直接改文件里的阈值也一样：正在运行的 `tsen run` 一分钟内读到，并推一条“监控范围已更新”，里面写明哪个资产从多少改成多少。
   2026-10-24 改成新规则后的实测（10-09～10-23，全部时段）：共 102 条，原来的规则 110 条；持仓 71 → 57 条（盘前 04:00 的 3% 关口少了很多），
   同时抓到了原来漏掉的冲高回落 / 探底回升（如 MU 10-22 从 -0.6% 34 分钟跌到 -3.6%、10-20 从 -2.4% 46 分钟反弹到 +0.7%）。
4. 联动关系也会变（现在的数字来自 AI 行情的一年）：周报末尾会附“联动体检”，只在同业 / 夜间信号的联动明显变弱、或冒出新的强联动候选时才附；
   `tsen scope check`（飞书 `/scope check`）随时看。只给建议，不自动改。

**全天候是怎么做到的**：美东 04:00–20:00 直接盯美股（yfinance 的盘前盘后分钟线）；美东 20:00–04:00 的美股夜盘没有免费数据，
这段时间由海外同一家公司 / 同业接力（主题里的 `overnight`：存储芯片盯首尔的 SK海力士正股，它涨跌 5%，SKHY 第二天盘前大概率跟着动，
推送里会注明是哪些持仓的夜间信号）；
期货、加密货币按各自的交易时段。盘前盘后成交稀疏、常有零星错价（实测：收盘集合竞价的成交 17:21 才补报，分钟线上凭空 -2% 再弹回），
所以这两个时段用最近 3 根 K 线的中位数判断，单笔错价不会触发，真实的移动只晚 1–2 分钟。

## 4. 长期运行

- **在 Mac 上运行**（默认配置就是为本机 LM Studio 设计的）：合上盖子或休眠会暂停程序，可以用
  `caffeinate -i tsen run` 防止系统空闲休眠。醒来或重启后程序会补上错过的简报（盘前简报只在开盘前补）、事件刷新和当晚汇总
  （每 15 分钟检查一次）；异动监控醒来后接着跑，但休眠期间的异动无法补测。注意 `caffeinate -i` 挡不住合盖休眠。
- **部署到别的机器**（每月约 5 美元的 VPS，或家里的小主机）：那里没有本机的大模型，需要把 `llm.tasks` 改成
  `[anthropic]` 或 `[openai]`（并在 `.env` 填对应密钥），或者把 `llm.providers.local.base_url` 指向能访问到的 LM Studio
  并删掉 `lmstudio` 那几行。用 systemd 守护进程：

  ```ini
  # /etc/systemd/system/tickersentinel.service
  [Unit]
  Description=tickersentinel
  After=network-online.target

  [Service]
  WorkingDirectory=/opt/tickersentinel
  ExecStart=/opt/tickersentinel/.venv/bin/tsen run
  Restart=always
  RestartSec=30

  [Install]
  WantedBy=multi-user.target
  ```

  程序重启后，会从数据库恢复当天已经推送过的异动，不会重复推送。

## 5. 数据来源

| 内容 | 来源 | 费用 |
|---|---|---|
| 行情（实时主源 / 海外） | Yahoo Finance（yfinance） | 免费，非官方接口 |
| 行情兜底、日线官方收盘价 | Alpaca（实时 IEX / 15 分钟以前的 SIP） | 免费账户；实时主源仍用 yfinance（IEX 盘前盘后只有美东约 08:00–17:00） |
| 公司新闻、财报日历 | Finnhub | 免费 key |
| 英 / 韩 / 中新闻 | Google News RSS | 免费 |
| 美股公告、内部人拟减持（Form 144）、SKHY 业绩发布时间（6-K） | SEC EDGAR | 免费 |
| 财报日（全部代码，多为估计） | yfinance | 免费 |
| 东京上市公司财报日（铠侠，官方） | 日本交易所集团「決算発表予定日」 | 免费 |
| 宏观日程 | 美联储、BLS、BEA、人口普查局、美国财政部、日本央行的官方日程；期权到期、休市按交易所日历推算 | 免费 |
| 简报、归因、事件汇总、对话 | 本机 LM Studio（默认）/ Claude API / OpenAI | 本地免费；云端按用量计费 |

## 6. 开发

```bash
uv pip install -e ".[dev,feishu]"   # 装上飞书 SDK，否则飞书相关的测试会被跳过
pytest -q                           # 全部测试离线运行，不需要网络和 key
```

当前状态：飞书推送、回复挂接与对话已在真实飞书上跑通；对话第二版（5 个查询工具）已用本地模型实测；
飞书日历 / 多维表格同步已实现，只做过离线测试。
接下来：阈值校准 → 期权监控 → 事件日历扩展（韩国 DART 等）；Telegram 之后再做。详见 CLAUDE.md 的路线图。

## 许可

[MIT](LICENSE)。
