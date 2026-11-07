# tickersentinel

[English](README.en.md) | 中文

按你自己的持仓定制的盯盘与投研小助理，在自己的电脑上常驻运行。平时不出声，只在需要你知道的时候推送：
股价急涨急跌时马上推一条，接着自动检索新闻、公告和日程，给出最可能的原因；每天定时发简报，每晚汇总接下来的财报和宏观日程。
推送发到飞书，也可以直接在飞书或本机网页里提问。持仓和板块联动全部写在配置文件里，代码里没有写死任何行业或股票。

- **全天候异动监控**：默认每 60 秒检测一次，**只有出现急涨急跌才推送**，没有异动就没有消息。美股按 24x5 盯（美东周日 20:00 到周五 20:00）：
  盘前、盘中、盘后用 Yahoo 的分钟线，20:00–04:00 的夜盘用 Hyperliquid 上 trade.xyz 的股票合约价格。海外股票按当地交易时段，
  期货、加密货币按各自的时段。同一板块的几只一起动时合成一条推送；每条推送末尾附一段宏观快照（美债利率、原油、金银、股指、VIX……）。
- **自动归因**：异动推出去之后，大模型根据异动前后的新闻、SEC 公告和日程判断原因，先分清是板块带的还是个股自己的，
  作为回复挂在那条异动下面；找不到明确原因就直说。
- **定时简报**：盘前、收盘后、每周一份。
- **事件日历**：提前收集未来 100 天的财报日、内部人拟减持（Form 144）、美联储议息、CPI / 非农等数据公布、国债拍卖、休市……，
  每晚汇总推送，也写进简报和归因。
- **飞书推送与对话**：推送、回复挂接都走飞书机器人（长连接，不需要公网地址）。给机器人发消息就能提问，它会自己查宏观数据、
  走势、新闻、异动记录和日历再回答。可选：把事件日历同步成飞书日程、把每条异动写进飞书多维表格，方便事后标注真实原因。
- **本机网页**：总览（对话、消息流、宏观看板）、异动列表与详情、事件日历、宏观趋势页。只给本机用，可以在 Chrome / Safari 里装成独立应用。
- **本地分钟线库**：宏观变量和持仓的 1 分钟线每两分钟存一次，不受 Yahoo“1 分钟线只保留约 30 天”的限制。
- **大模型默认用本机**：LM Studio 里的 Qwen 27B，不花钱，数据不出本机；也可以按任务切到 Claude 或 OpenAI。
  没有大模型时一切照常运行，简报和归因降级成新闻列表。

设计细节、每个决定的理由和实测数字见 [CLAUDE.md](CLAUDE.md)（开发笔记，很长）。

> **免责声明 / Disclaimer**：本项目仅供个人学习与研究，**不构成任何投资建议**。行情来自免费或非官方的数据源（如 Yahoo Finance），
> 可能延迟、缺失或出错；异动归因由大模型根据检索到的新闻生成，可能不准确。据此做出的任何交易决定由使用者自行负责。
> 使用时请遵守各数据源（Yahoo、SEC EDGAR、Hyperliquid、飞书开放平台等）的服务条款和访问频率要求。

## 快速开始

不需要任何 key：行情、新闻、公告、宏观日程都有免费来源。需要 Python ≥ 3.11（推荐 3.12）。

```bash
git clone https://github.com/fengchaoya/tickersentinel.git && cd tickersentinel
uv venv --python 3.12 && uv pip install -e ".[dev,web]"   # web：本机网页；要飞书再加 feishu
cp .env.example .env               # 先不填也能跑；建议填 SEC_EDGAR_USER_AGENT 和 BLS_USER_AGENT（一个联系邮箱）
.venv/bin/tsen check               # 检查配置、密钥和各数据源
.venv/bin/tsen replay --days 5     # 用最近 5 个交易日的真实分钟数据回放，看看会推送什么
.venv/bin/tsen run                 # 常驻运行；网页在 http://127.0.0.1:8765
```

仓库里的示例配置持有一只光模块股（Lumentum，LITE），推送渠道是 `console`（只打印到终端，同时写进 `data/outbox.log`）。
换成自己的持仓、接上飞书和本地大模型，见下面的「配置」。

- [uv](https://docs.astral.sh/uv/) 会自动下载合适的 Python（`brew install uv`，或 `curl -LsSf https://astral.sh/uv/install.sh | sh`）。
  不用 uv：`python3 -m venv .venv && .venv/bin/pip install -e ".[dev,web]"`。
- 一共四组可选依赖：`web`（本机网页）、`feishu`（飞书推送、对话、日历 / 表格同步）、`dev`（测试）、`telegram`（还没实现）。
  没装 `web` 时 `tsen run` 照常运行，只是没有网页；没装 `feishu` 时飞书推送退回终端打印。
- **请用 `-e` 在克隆下来的目录里安装**：程序按包所在的位置找项目目录（`config/`、`.env`、`data/`），所以在任何目录下运行 `tsen` 都可以。
  包装在别处时，用环境变量 `TICKERSENTINEL_HOME` 指向项目目录（要在 shell 里设置，不能写进 `.env`：`.env` 本身就是在项目目录里找的）。

## 配置

### 配置文件分三处

| 文件 | 写什么 |
|---|---|
| `config/assets.yaml` | 持仓（以及只盯盘、不算持仓的资产）、每只的新闻检索词和备注、警报灵敏度（按资产类别给默认值，单个资产可以覆盖） |
| `config/themes/<主题>.yaml` | 板块联动，一个板块一个文件：板块基准、同业、夜间信号、相关公司、板块新闻检索词、板块指标。同一板块的持仓共用 |
| `config/settings.yaml` | 与具体股票无关的设置：时区、简报时间、检测频率与冷却、事件日历、基础宏观变量、数据源、大模型、推送渠道、网页、分钟线库 |

示例里生效的主题是「光模块」（`optics.yaml`，LITE 引用它）；「存储芯片」（`memory.yaml`，首尔 / 东京的夜间信号都写好了）是备用模块，
没有持仓引用它，所以不生效。想试试，去掉 `assets.yaml` 里 MU 那几行的注释即可。`tsen scope` 一屏看完现在的监控范围。

所有字段写错名字都会直接报错（不会悄悄忽略）。`assets.yaml` 和 `themes/` 改了以后，正在运行的 `tsen run` 一分钟内自动读到，
推一条“监控范围已更新”；`settings.yaml` 改了要重启。

**用自己的持仓**：把 `config/` 整个复制成 `config.local/`（已在 `.gitignore` 里），让程序读那一份。这样持仓不会随 git 公开，
拉取更新时也不会和示例配置冲突；`tsen scope add / tune` 也写到那里。

```bash
cp .env.example .env                      # 还没有 .env 的话
cp -R config config.local
# 然后把 .env 里 TICKERSENTINEL_SETTINGS=config.local/settings.yaml 那一行的注释去掉
```

读哪份配置：命令行 `tsen --config 路径` > 环境变量 `TICKERSENTINEL_SETTINGS`（可以写在 `.env` 里）> `config/settings.yaml`。
`assets.yaml` 和 `themes/` 默认和 settings.yaml 在同一个目录，也可以用 `TICKERSENTINEL_ASSETS` / `TICKERSENTINEL_THEMES` 单独指定。
环境变量里的相对路径按项目目录算，`--config` 的相对路径按当前目录算。

### 密钥：`.env`

密钥和个人信息只放项目根目录的 `.env`（不进 git；`.env.example` 是空白模板）。等号后面直接填值。**都不填也能运行**，对应功能自动跳过或降级：

| 变量 | 用途 | 不填会怎样 |
|---|---|---|
| `SEC_EDGAR_USER_AGENT` | SEC 要求请求头带联系方式，填“项目名 + 你的邮箱” | 用占位邮箱，SEC 可能限流或拒绝 |
| `BLS_USER_AGENT` | 美国劳工统计局（BLS）发布日程的请求头，格式同上（可以和 SEC 那行一样） | 日历里没有 CPI、PPI、非农、JOLTS 的公布时间；问“下周有没有 CPI”时机器人会答没有。`tsen check` 会提示 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_CHAT_ID` | 飞书推送与对话（三项都要）；日历 / 多维表格同步只要前两项 | 推送退回终端打印，不能在手机上提问 |
| `FINNHUB_API_KEY` | 公司新闻、财报日历（finnhub.io，免费注册） | 少一个新闻源；财报日照样从 yfinance 拿 |
| `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` | 行情兜底（Yahoo 出错或缺代码时补上）、日线换成官方收盘价（alpaca.markets，免费账户） | 只用 Yahoo，不设兜底 |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | 云端大模型，要在 `llm.tasks` 里选用才会调用 | 只用本地模型 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`、`OPENDART_API_KEY` | 还没实现，留空 | — |
| `TICKERSENTINEL_SETTINGS` | 可选，指向自己的配置（见上） | 读 `config/settings.yaml` |

### `settings.yaml` 里常改的

| 段 | 默认值 |
|---|---|
| `timezone` | 本地时区（IANA 名称）。示例写的是 `America/New_York`，先改成你自己的。影响简报、事件刷新和每晚汇总的时间，“明天”是哪天，推送和网页上显示的时间，飞书多维表格的时区。异动检测按各交易所自己的日历，和它无关 |
| `briefs` | 盘前 07:30（只在交易日）、收盘后 15 分钟（提前收盘日跟着提前）、周报周六 09:00（有建议时末尾附“联动体检”） |
| `monitor` | 每 60 秒检测一次（`interval_seconds`）；同主题合并推送 10 分钟（`merge_minutes`，0 = 每只单独推）；冷却 15 分钟等检测参数在 `monitor.price` |
| `events` | 每天 06:40、18:30 刷新，20:00 汇总；收集未来 100 天（`lookahead_days`）；宏观日程 `events.macro`；手工录入 `events.manual` |
| `macro` | 基础宏观变量 `indicators`（见下面「宏观数据」） |
| `sources` | 行情主源 yfinance、兜底 Alpaca（`sources.prices`）；夜盘 `sources.night.provider: hyperliquid`（`none` 关掉）；新闻源 |
| `llm` | 大模型供应商 `providers` 和每个任务用哪个 `tasks`（见下） |
| `notify` | 推送渠道 `provider: console \| feishu`；`feishu_domain: feishu \| lark`；飞书同步 `feishu_sync` |
| `web` | `enabled`、`port: 8765`、`up_color: red`（红涨绿跌，`green` 反过来）；`host` 只能是本机回环地址 |
| `bars` | 分钟线库：每 120 秒采集、新代码补 29 天、超过 120 天的压成 5 分钟线 |
| `storage` | 数据库路径（默认都在 `data/` 下） |

定时简报按本地日期安排，目前按美洲时区设计；在亚洲 / 欧洲时区下，盘前、收盘简报对应的交易日可能会错开，需要自己调整时间。

### 本地大模型（LM Studio）

默认所有大模型任务（简报、归因、事件汇总、对话）都用本机 LM Studio 里的 `qwen/qwen3.8-27b`（4bit，约 16 GB 内存）。

1. 装 [LM Studio](https://lmstudio.ai)，在 Discover 里搜索下载这个模型（或 `~/.lmstudio/bin/lms get qwen/qwen3.8-27b`）。
2. **不用手动启动服务**：程序每次调用前会检查，需要时自己执行 `lms server start` 和 `lms load`（上下文 32768，闲置 60 分钟自动卸载）。
   `lms` 默认在 `~/.lmstudio/bin/lms`，不需要在 PATH 里；装在别处时改 `llm.providers.local.lmstudio.lms_path`，
   不想让程序管 LM Studio 就设 `auto_start: false`。
3. “思考”模式是关着的（`reasoning_effort: none`）：整理新闻不需要长推理，快好几倍。作者本机（M5 Pro / 64 GB）实测：
   一次归因约 15 秒，一份简报约 40 秒，一次对话十几到五十秒。

- 换模型：改 `llm.providers.local.model`。换成 Ollama 等其他 OpenAI 兼容的服务：改 `base_url` 和 `model`，删掉 `lmstudio` 那一段。
- 本地模型不能联网检索，归因只依据程序收集到的新闻和公告。想让 Claude 联网补充检索：`llm.tasks.attribution` 写成
  `[anthropic]`（只用 Claude）或 `[local, anthropic]`（本地失败时才用），并在 `.env` 填 `ANTHROPIC_API_KEY`。
- 每个任务可以列几个供应商，依次尝试，都失败才降级。

### 推送：飞书

推送和回复都走飞书机器人，用长连接收消息，**不需要公网地址或服务器**。一次性设置约 15 分钟：

0. **安装 SDK**：`uv pip install -e ".[feishu]"`。用国际版 Lark 的话，把 `notify.feishu_domain` 改成 `lark`（推送和日历 / 表格同步共用）。
1. **创建应用**：[飞书开放平台开发者后台](https://open.feishu.cn/app)（Lark：open.larksuite.com/app）→「创建企业自建应用」。
   需要你在一个有管理员权限的飞书企业 / 团队里；没有的话可以在飞书客户端里免费新建一个团队。
2. **开通机器人**：应用详情 →「添加应用能力」→「机器人」。
3. **拿到凭证**：「凭证与基础信息」页的 App ID、App Secret，写进 `.env` 的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`。
4. **开通权限**：「权限管理」里开通 `im:message`、`im:message:send_as_bot`、`im:message.p2p_msg:readonly`（读用户发给机器人的单聊消息）、
   `im:message.group_at_msg:readonly`（接收群聊里 @ 机器人的消息）。
5. **建立长连接**：终端运行 `tsen feishu setup`（只需要 App ID 和 Secret），看到“已连接飞书”后**保持它运行**。
6. **订阅事件**：「事件与回调」→「事件配置」→ 订阅方式选「使用长连接接收事件」并保存（后台会检测第 5 步的连接），
   再「添加事件」→ `im.message.receive_v1`。
7. **发布**：「版本管理与发布」→ 创建版本 → 可用范围选你自己 → 申请发布。**以后改权限都要重新发布一个版本才生效。**
8. **查 chat_id**：在飞书里给机器人发一句话（想发到群里就拉进群并 @ 它）。第 5 步的终端会打印 `FEISHU_CHAT_ID=oc_...`，
   机器人也会回复这个值。写进 `.env`，然后 Ctrl+C 结束。同时打印的“发送人 open_id”（`ou_...`）是你自己的飞书 ID，设置日历 / 表格时可能用到。
9. **验证**：`tsen feishu test` 发一条测试消息和一条挂在它下面的回复；`tsen check` 显示机器人名称。
10. **切换渠道**：`settings.yaml` 里 `notify.provider: feishu`。之后所有推送都到这个会话，其他会话的消息一律忽略。
    三项密钥缺一项或没装 SDK 时，只在日志里警告一句并退回终端打印，不会启动就崩溃；启动消息里的“推送渠道”一行写的是实际在用的渠道。

### 飞书日历与多维表格

可选，默认关闭。只需要 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，和推送渠道是不是飞书无关。

- **事件日历 → 飞书日历**：财报日、拟减持、解禁、宏观日程同步成飞书日程（有精确时间的是 30 分钟的日程，只有日期的是全天日程）。
  改期、确认更新同一个日程，过时的估计会删掉；`past_days`（7 天）以前的日程留着、不再更新。
- **异动 → 飞书多维表格**：每条异动 + 归因一行。「label 真实原因」「notes 备注」两列留给你事后填写，程序会拉回数据库的
  `alerts.label / notes`，用来评估归因准确率。

两者都由应用以自己的身份创建再共享给你：日历你是只读成员（它是事件日历的镜像，在飞书里改了会被覆盖；要更正日期写进 `events.manual`），
多维表格你有完整权限。

> 这部分有完整的离线测试（模拟飞书接口），第一次在真实飞书上设置时请留意命令的输出。

1. **开通权限**：「权限管理」开通 `calendar:calendar` 和 `bitable:app`，然后**发布一个新版本**。
2. **建日历**：`tsen feishu calendar-setup`。找出你的 open_id、建好日历「tickersentinel · 事件日历」、把你加为成员并同步一次。
   然后在飞书「日历」里订阅它（日历列表旁的「+」→「订阅日历」→ 搜索日历名；应用没法替你订阅）。
   不想订阅：设 `notify.feishu_sync.invite_user: true`，日程以邀请的形式直接出现在你的主日历里（不发通知）。
   之后再改回 `false`，已经加进主日历的日程不会自动移除。
3. **建表格**：`tsen feishu bitable-setup`。建好「tickersentinel · 异动记录」，打印链接，写入已有的全部异动。
4. **打开开关并重启**：`notify.feishu_sync.calendar: true`、`bitable: true`，重启 `tsen run`。之后每次刷新事件日历后同步日历；
   每次检测后在后台把新异动和刚完成的归因写进表格；每 15 分钟对一遍账，最多每小时拉回一次你填的标注。
5. **随时手动同步**：`tsen feishu sync`。偶尔跑一次没问题，但不要让它和 `tsen run` 长时间同时跑（比如放进 cron），两个进程会撞上飞书的并发写冲突。

其他选项（都在 `notify.feishu_sync`）：`calendar_name`、`bitable_name`、`calendar_role`（`reader` | `writer`）、
`event_minutes`（有时间的日程多长，默认 30）、`past_days`（同步多久以前的事件，默认 7）。

- **你的 open_id**：setup 命令会从 `FEISHU_CHAT_ID` 会话里自动找，但只在能确定是你的时候才用；会话是群聊（多人发过言或有多个成员）时不会去猜，
  否则可能把表格权限共享给别人。这时用 `--open-id ou_xxx` 指定，指定一次会记住。
- **标注列**：程序只更新自己生成的列。飞书里的空单元格不会覆盖库里已有的标注（防止列被改名或删掉后补建的空列把标注冲掉）；
  不要改列名；你删掉的行不会再写回。
- **出错时**：缺权限时命令会列出要开通的权限和后台链接。常驻运行时同步失败不影响推送和盯盘，失败的那部分 30 分钟内不再尝试，
  每一段连续失败只推一次提醒。

## 运行

### 先让终端找得到 `tsen`

`tsen` 装在项目的 `.venv` 里，新开的终端默认找不到它。任选一种：

```bash
# 一次设置，以后任何终端都能用（在项目目录里执行）
mkdir -p ~/.local/bin && ln -sf "$PWD/.venv/bin/tsen" ~/.local/bin/tsen
echo $PATH | tr ':' '\n' | grep -x "$HOME/.local/bin" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc

# 或者每次先激活虚拟环境
source .venv/bin/activate

# 或者写全路径
.venv/bin/tsen check
```

`tsen` = `tickersentinel` = `python -m tickersentinel`。所有命令的输出和推送文案都是中文。

### 常驻运行

```bash
caffeinate -is tsen run     # Mac 上防止系统休眠（-s 只在插着电源时有效；挡不住合盖休眠）
```

`tsen run` 一个进程里包括：定时简报、全天候异动监控、事件日历、分钟线采集、飞书长连接（对话）和本机网页。启动时推一条“已启动”，
写明今天的日程、推送渠道、行情源、各任务用的大模型、飞书同步状态和网页地址。Ctrl+C 退出（会先等正在进行的飞书同步写完，最多 30 秒）。

### 推送什么、什么时候

时间都按 `timezone` 的本地时间。

| 推送 | 时间 |
|---|---|
| 异动快讯 | 随时（默认每 60 秒检测一次）；只在急涨急跌时推 |
| 归因 | 异动之后，作为回复挂在那条异动下面 |
| 盘前简报 | 07:30，只在交易日 |
| 收盘简报 | 美股收盘后 15 分钟 |
| 周报 | 周六 09:00 |
| 事件汇总 | 20:00；只在“明天有事件”或“有新增 / 改期 / 确认”时推，第一次运行推一份完整日历 |
| 提醒 | 盘中检测连续失败 5 次、分钟线采集连续失败约 30 分钟、简报 / 事件日历生成失败、飞书同步失败、监控范围变了 |

看门狗每 15 分钟检查一次：日历超过 12 小时没刷新就刷新；电脑睡醒或程序重启后补发错过的简报（盘前简报只补到开盘前，
收盘简报收盘后 6 小时内，周报定时点后 12 小时内）和当晚的汇总。同一种简报每天最多一份。

### 命令一览

```bash
tsen check                  # 检查配置、密钥、各数据源、大模型、推送渠道，飞书同步开没开
tsen run                    # 常驻运行（见上）
tsen tick                   # 立即检测一次（只打印；加 --send 推送并入库）
tsen replay --days 5        # 按当前阈值回放最近 5 个交易日；--symbols LITE,night:optics:XSHG 只看几个代码
tsen brief premarket        # 生成一份简报：premarket / close / weekly（只打印；加 --send 才推送）
tsen events                 # 刷新事件日历并生成汇总（只打印；加 --send 才推送）
tsen macro                  # 宏观快照
tsen assets                 # 盯盘资产与警报标准，换算成几倍 σ
tsen scope                  # 监控范围：持仓 × 主题（= tsen scope show）
tsen scope add LITE         # 给新持仓找联动、出方案、回放预览，确认后写入（--theme 指定主题；--drop A,B / --keep C 调整候选）
tsen scope suggest LITE     # 只出方案，不写入
tsen scope remove LITE      # 移除持仓（主题文件保留备用）
tsen scope undo             # 撤销最近一次改动（之后文件又被改过时拒绝，--force 强制）
tsen scope check            # 联动体检
tsen scope tune LITE        # 调警报阈值：按日波动建议，或 --day 5,8,12 --rapid 60:3；回放对比改前改后，确认后写入
tsen scope prompt LITE      # 打印给 Claude Code 准备联动候选的上下文（项目技能 add-holding 用）
tsen ask "LITE 今天为什么跌？"  # 在终端里提问（只读预览，不留记录）；加 --keep 用正式库、记住上下文（--keep /new 清空）
tsen web                    # 打开本机网页（见下）
tsen bars                   # 分钟线库：每个代码存到了哪天
tsen bars --backfill        # 不开 tsen run 也能补最近 29 天（--days N、--symbols A,B）；--compact 立即压缩旧数据
tsen feishu setup           # 飞书：建立长连接，打印 chat_id 和你的 open_id
tsen feishu test            # 飞书：发测试消息和回复
tsen feishu calendar-setup  # 飞书：建事件日历并共享给你（群聊时加 --open-id ou_xxx）
tsen feishu bitable-setup   # 飞书：建异动多维表格并共享给你
tsen feishu sync            # 飞书：立即同步日历、补齐表格、拉回标注
```

全局参数：`--config 路径`（用哪份 settings.yaml）、`-v`（调试日志）。每个命令的全部参数用 `tsen <命令> --help` 查。
`scope add / tune` 不是在交互终端里运行时只预览、不写入，加 `--yes` 才写；`--replay-days N` 改回放天数（0 = 不回放）。

**手工录入事件**（锁定期到期、股东大会等）：写在 `settings.yaml` 的 `events.manual` 里，例如
`- {symbol: LITE, day: 2027-01-05, kind: lockup_expiry, title: "某次增发的锁定期到期"}`。

## 本机网页

`tsen run` 启动时顺带开一个只给本机用的网页，默认 http://127.0.0.1:8765。需要装 `web` 这组依赖；没装、端口被占用或
`web.enabled: false` 时，启动消息里会写明原因，盯盘照常。

| 页面 | 看什么 |
|---|---|
| 总览 `/` | 左边两个选项卡：**对话**（直接提问，见「对话」）和**消息流**（每条推送实时出现，归因挂在对应的异动下面；停在对话时选项卡上显示未读数；每条推送都有“问一下”按钮，带着这条推送的全文提问）。右边：宏观看板（实时快照，每行带最近 24 小时的小走势线，财政部收益率曲线带近 90 天走势）、今明两天的日程、今天的异动 |
| 异动 `/alerts` | 按时间范围、代码、时段、规则、是否已标注筛选；导出 CSV（Excel 能直接打开） |
| 异动详情 `/alerts/编号` | 分钟走势图（最多前 3 小时到后 90 分钟、不超出这一段，标的、板块基准和几只同业都按“较参照价 %”画，标出触发点）、板块 / 个股拆分、宏观同期变化（从这波行情起点到异动，哪些变量动得最反常）、当时的宏观快照、归因、前后日程、同一波行情的前后几条；在这里填“真实原因”和备注 |
| 日历 `/calendar` | 月视图和列表视图（14 / 30 / 60 / 100 天）；全部 / 宏观 / 公司 / 持仓筛选；宏观与公司分色，持仓加粗，“预计”的日期虚线框；点开看详情 |
| 宏观 `/macro` | 各宏观变量和持仓的趋势图、“谁动了”排行、对比视图、收益率曲线（见下） |

**宏观页**（数据全部来自本地分钟线库，不现取）：

- **窗口**：今天 / 24 小时 / 5 天 / 1 个月 / 自定义；从异动详情点“在宏观页查看”打开那条异动前后的窗口。
  休市时（周末、节假日）预设窗口截到最近收盘，页面上可以切到“截到现在”。
- **趋势小图**：纵轴是实际值（原油 104、VIX 19.4、收益率 4.93%），虚线是窗口起点，标题旁是最新值和较起点的变化（利率用 bp，其他用 %）。
  时间轴对齐、十字线联动，异动画成标记，没有交易的时段断开不连线。VIX、KOSPI 这类只在交易时段有数据的变量，各段直接接起来（标“交易时段”）。
  股指在美股休市时换成期货或 ETF（`off_hours_symbol`）。美债 10 年 / 30 年收益率在 Yahoo 上只有美东白天的数据，夜里用国债期货的涨跌折算补上，
  画成虚线（`night_proxy: null` 关掉）。
- **谁动了**：每个变量的变化除以它平常的波动（z 值）排序，利率、原油、VIX 放在一起能比；在图上拖选一段就只算那一段。
- **对比**：最多 8 个变量放进一张图，按较起点的变化画。纵轴可选“% / bp”（价格类右轴、收益率左轴，0 对齐）或“几倍日波动”。
  选择会记住，也写在网址里（`?cmp=wti,gold,hold.LITE`），可以收藏。
- **持仓**：`assets.yaml` 里的持仓是最后一组，一起排“谁动了”。
- **现在补抓**：立即采集一轮分钟线。

**其他**：

- **只限本机**：只监听回环地址，不需要登录。只接受 Host 是本机地址的请求；写操作要带页面里的随机口令，其他网站没法借你的浏览器调用它。
  `web.host` 写成别的地址会直接报错。部署在别的机器上时用 `ssh -L 8765:127.0.0.1:8765 那台机器` 在本机打开。
- **单独打开**：`tsen web [--port N] [--no-open]`。`tsen run` 已经在运行就直接打开浏览器；没在运行时单独起一个网页：不检测、不定时推送，
  但网页上的提问、标注、“现在补抓”照样写进正式库（在网页对话里确认的 `/scope` 改动也照样写入配置）。端口被别的程序占着时会提示换一个端口。
  `tsen web --preview` 用正式库的内存副本，什么都不保存（提问照样能答，补抓不可用），随便点。
- **顶栏小标**：运行中 · 上次检测时间 / 检测出错 / 采集出错 / tsen run 未运行（超过 3 分钟没有检测）；悬停看启动时间、上次检测和分钟线采集。
- **装成独立应用**：Chrome 菜单“投放、保存和分享 → 将页面作为应用安装”（或地址栏右侧的安装图标）；Safari 菜单“文件 → 添加到程序坞”。
- 涨跌颜色默认红涨绿跌（`web.up_color: green` 反过来），右上角也能临时切换；浅色 / 深色跟随系统，也能手动切换。
- 异动走势图先读本地分钟线库，库里没有的才去 Yahoo 现取；Yahoo 的 1 分钟线只保留约 30 天，更早又不在库里的异动只显示数字、不画图。
- 消息流记录每一条推送（包括没送达的）；有这张表之前的推送从异动和简报记录里补上，标“历史记录”。

## 对话

两个入口，背后是同一个机器人和同一套工具：

- **飞书**：`tsen run` 在跑、推送渠道是飞书时，在 `FEISHU_CHAT_ID` 这个会话里给机器人发消息（群里要 @ 它）。它先回“收到，思考中…”再作答。
- **本机网页**：总览页的“对话”选项卡，`tsen run` 或 `tsen web` 在跑就能用，不需要飞书。用飞书推送时，网页和飞书是**同一个会话**
  （上下文共用，飞书里的问答也显示在网页上）；网页上的问答不会发到飞书。没用飞书时网页有自己的会话。

凡是涉及最新情况的问题，模型都会先调用下面 5 个只读工具，数字和日期只用工具查到的，查不到就直说：

| 工具 | 查什么 | 可以这样问 |
|---|---|---|
| `get_macro` | 宏观快照（利率、原油、金银、股指、VIX、美元、比特币……） | “现在长端利率和油价怎么样” |
| `get_intraday` | 今天（休市时为最近交易日）较昨收和最近 N 分钟的涨跌，拆成板块部分（β × 基准）和个股特异部分，附同业 | “LITE 为什么跌”“是板块带的吗” |
| `search_news` | 最近的新闻和 SEC 公告（最多 10 条，给公告留 4 个名额）；持仓用预设的检索词，其他关键词按提问的语言查 Google News | “最近有什么新闻”“这周有没有新的 Form 4” |
| `recent_alerts` | 最近推送过的异动、拆分和当时的归因 | “上次那次急跌是什么原因” |
| `upcoming_events` | 事件日历：财报日（预计 / 已确认）、拟减持、解禁和宏观日程 | “下次财报哪天”“接下来两周有哪些事件” |

- 它知道你的持仓（含 `assets.yaml` 里每只的 `note`，可以写模型不知道的新背景）、你“回复”的那条推送、最近 5 轮对话（`chat.history_turns`）。
- 代码和名称都认：中文 / 韩文名可以只写一部分，英文名按完整单词匹配，所以 ON、AD 这类短代码按代码本身查。
- 新闻引用由程序换成真实链接，模型自己写的网址会去掉，不会出现编造的链接。对话不联网检索。
- 它只回答问题：不能设提醒、不能下单；涉及买卖时给分析框架，不给指令。

**不经过大模型的命令**（几秒钟就回）：

- `/new`（或 `/reset`、“新对话”）：开始新对话。
- `/macro`（或 `/m`、“宏观”、“宏观数据”）：一份宏观快照。
- `/scope`（或“监控范围”）：看现在的监控范围。
- `/scope add 代码 [主题]`、`/scope suggest 代码`（只出方案）、`/scope remove 代码`、`/scope undo`、`/scope check`、
  `/scope tune 代码 [全天 5 8 12] [急涨 60:3]`：和命令行的 `tsen scope` 一样。会改配置的，先回一份方案，你回“确认”（或确定 / 是 / 好 / yes）
  才写入，回“取消”（或算了 / 不 / no）放弃，回“去掉 X”“加上 X”先调整候选；30 分钟内有效。确认后立即生效，不用重启。

在终端里测试同一套逻辑：`tsen ask "问题"` 把正式库复制进内存，先刷新一次事件日历再回答，问答不保存；`tsen ask --keep "问题"`
用正式库，记进一个“终端”会话，可以连续追问。

## 监控规则与灵敏度

### 盯哪些资产：`config/assets.yaml`

```yaml
defaults:                       # 每类资产的默认标准；单个资产可以在自己的 alert 里覆盖
  us_equity: {hours: 24x5, rapid_pct: {5: 1.5, 15: 2.5, 30: 3, 60: 3}, z: 4.0, day_pct: [5, 8, 12]}
  future:    {rapid_pct: {5: 0.6, 15: 1.0, 30: 1.4, 60: 1.4}, z: 4.0, day_pct: [1.5, 2.5, 4]}   # 黄金一天 ±2% 已经是大行情
assets:
  - {symbol: LITE, name: Lumentum, class: us_equity, theme: optics, queries: {en: ["Lumentum"], zh: ["Lumentum 光模块"]}}
  # - {symbol: "GC=F", name: 黄金, class: future}
```

- 资产类别 `class`：`us_equity`（美股 / ADR）、`foreign_equity`（海外股票）、`future`（CME 期货）、`crypto`。
  ADR 可以写 `underlying`（主上市代码）、`adr_ratio`、`fx_symbol`。`holding: false` 的是只盯盘、不算持仓的资产。
- 美股的时段 `hours`：`24x5`（示例的默认：再加美东 20:00–04:00 的夜盘，周日晚到周五）| `extended`（04:00–20:00，盘前 + 盘中 + 盘后；
  删掉 defaults 里那一行时的默认）| `regular`（只盯常规时段）。`sources.night.provider: none` 时 24x5 等于 extended。

| 资产 | 怎么切段 | 涨跌参照 |
|---|---|---|
| 美股 | 夜盘 20:00–04:00（Hyperliquid）、盘前 04:00–开盘、盘中、盘后 收盘–20:00（美东）；周五 20:00 到周日 20:00 不盯 | 盘后 = 当天收盘；夜盘、盘前、盘中 = 前一交易日收盘 |
| 期货 | CME：美东前一天 18:00 到当天 17:00 | 上一段 17:00 的价格 |
| 加密货币 | UTC 自然日 | UTC 0 点 |
| 海外股票 | 当地交易所的常规时段（含午休） | 当地昨收 |

### 两条规则

幅度都按参照价的百分点算（“+2% 跌到 -1%”是 3 个点）：

- **急涨急跌** `rapid_pct`：{窗口分钟: 下限%}。最近这么多分钟里的**最高点**到现价跌了这么多（或最低点到现价涨了这么多），
  同时 z 值 ≥ `z`（相对它自己近 5 天的波动足够反常，波动大的日子自动收紧）。看高低点而不是窗口起点，所以“先冲高、再跌回水下”能抓到。数字越小越灵敏。
- **全天累计** `day_pct`：从参照价和今天（含参照价相同的更早几段）的最高点里更高的那个算起，跌了这么多（涨的方向对称），每一档推一次，
  创出新高 / 新低后重新计。管开盘跳空和几个小时的一路阴跌。

另有一条“个股特异”（剔除板块后的异常收益），默认关闭。

- 每段开头 10 分钟不跑急涨急跌（开盘竞价噪声大，跳空交给全天累计）；盘后段例外，财报常在 16:05 发布，开头几分钟正要盯。午休后复盘也先预热。
- 盘前、盘后、夜盘成交稀疏、常有零星错价，用最近 3 根 K 线的中位数判断，单笔错价不会触发，真实的移动只晚 1–2 分钟。
- 同一波不重复推：同方向 15 分钟（或窗口长度）内只推一次，幅度扩大到 2 倍才再推一条“扩大”；急涨急跌刚推过的，全天那一档不再单独推。
  参照价相同的几段连成一串（周一盘后、周二夜盘、盘前、盘中都以周一收盘为参照），盘后推过“跌超 5%”，夜里和第二天早上不会再推。
- 同一主题的几只同方向一起动：合成一条推送、归因一次；之后 10 分钟内跟着动的挂在第一条下面作回复。没写主题的资产不合并。
- 推送失败的异动 30 分钟内持续补发（标题注明“补发”）。重启后从数据库恢复冷却状态，不会重复推送。

### 板块联动：`config/themes/`

“加一只股票”简单，难的是和它联动的那一圈。这些按板块写成“主题”，一个文件一个（文件名就是主题名），持仓用 `theme:` 指向它。
**没有持仓引用的主题不生效**（不取数据、不收集事件、不推送），所以可以先放着备用；卖掉最后一只持仓，这一整圈自动停掉。

```yaml
# config/themes/optics.yaml（示例里 LITE 引用它）
name: 光模块
focus: 数据中心光模块与光器件（800G / 1.6T、CPO、硅光）     # 写进提示词
benchmark: basket                 # 板块基准：ETF / 指数代码，或 basket（peers 里美股的等权篮子）
peers: {COHR: Coherent, AAOI: Applied Optoelectronics, CIEN: Ciena, FN: Fabrinet, GLW: 康宁, VIAV: Viavi}   # 同涨同跌
overnight: {"300308.SZ": 中际旭创, "300502.SZ": 新易盛, "300394.SZ": 天孚通信}   # 夜间信号：自动成为只盯盘的资产
overnight_alert: {rapid_pct: {5: 2.5, 15: 4, 30: 5, 60: 5}, day_pct: [5, 8, 12]}  # 夜间篮子的阈值
catalysts: {NVDA: 英伟达, ANET: Arista, AVGO: 博通, MSFT: 微软}  # 大客户 / 供应商：只进事件日历和归因
news: {en: ["optical transceiver"], zh: ["光模块"]}          # 板块新闻检索词
indicators:                       # 板块指标：只附在这个板块的异动推送上
  - {key: cny, name: 美元/人民币, symbol: "CNY=X", kind: level, ref: 24h}
```

四种联动分开放：同涨同跌的放 `peers`，用来拆分“板块 / 个股”和列同期涨跌；会牵动板块、但平时不同涨同跌的大客户放 `catalysts`
（英伟达和 LITE 剔除大盘后的日收益相关只有 0.29，放进 peers 会把拆分带偏）。同一交易所有两只以上夜间信号时，合成一个等权篮子触发，
一波行情只推一条；某一只自己动 ≥ 3% 且相对篮子也偏离这么多时才单独推（`overnight_single_pct`；`overnight_basket: false` 改成每只单独推）。

**不想手写**：`tsen scope add 代码`（飞书 `/scope add 代码`）会替你找候选（Yahoo 的相似股票和行业龙头、Finnhub、已有主题），
取近一年日线逐个打分（同业和基准看最近 120 个交易日，夜间信号看最近 160 个；同业：剔除 SPY 后的残差相关 ≥ 0.5；基准：在行业 ETF 和同业篮子里挑 R² 高的；夜间信号：海外股票当天涨跌和
它次日开盘跳空的偏相关 ≥ 0.3），按它的日波动换算警报阈值，和已有主题联动够强就并进去。写入前按建议阈值回放几天，告诉你大概推多少条；
写入先备份到 `data/scope_history/`，`tsen scope undo` 撤销。

同业、海外对应公司、大客户这类“提名”要靠模型自己的知识，本地模型不够用，默认交给 **Claude Code**（用订阅额度，不调用 API）：
在本项目里打开 Claude Code，说“加一只持仓 LITE”，项目技能 `add-holding`（`.claude/skills/`）会联网核实代码，写好
`data/scope_candidates/LITE.json`，再运行 `tsen scope add` 出方案，你确认后才写入。没准备候选时只用其他来源。
想让程序自己调用线上 Claude，把 `llm.tasks.scope` 写成 `[claude_research]` 并填 `ANTHROPIC_API_KEY`。

### 调灵敏度

1. `tsen assets`：每个资产的阈值换算成“相当于它最近几倍的波动（σ）”。同样是 5 分钟 1.5%，对个股是正常的阈值，对黄金几乎永远不会触发。
2. `tsen replay --days 10`：按当前阈值回放最近 10 个交易日（含盘前、盘后、夜盘、海外时段），看会推多少条、都在什么时候。
3. `tsen scope tune 代码`：按最近 120 个交易日的日波动给建议阈值（默认值按日波动 5% 的股票定，期货按 2%），也可以直接给数字
   （`--day 5,8,12 --rapid 60:3`；飞书里写“全天 5 8 12 急涨 60:3”；只改写出来的窗口，`60:0` 去掉这一档）。回放对比“原来 / 改后”，
   确认后写到该写的地方（持仓写进 `assets.yaml`，夜间篮子写进主题的 `overnight_alert`）。纯计算，不用大模型。
4. 联动关系会随行情阶段变化：周报末尾的“联动体检”只在同业 / 夜间信号明显变弱、或冒出新的强联动候选时才附；`tsen scope check` 随时看。只给建议，不自动改。

完整的检测设计（为什么这样算、实测数字）见 CLAUDE.md 的「关键设计决策」。

## 宏观数据

每条异动推送末尾附一段宏观快照，归因模型也会参考。随时查看：飞书或网页里发 `/macro`，终端里 `tsen macro`。变量在 `macro.indicators` 里增删：

| 变量 | 数据 | 涨跌口径 |
|---|---|---|
| 美债 10 年 / 30 年 | 收益率指数 `^TNX` / `^TYX`，约延迟 15 分钟 | 较昨收，bp |
| 美债 2 年、2s10s 利差 | 美国财政部每日收益率曲线（官方） | — |
| WTI 原油、黄金、白银 | CME 连续合约 `CL=F` / `GC=F` / `SI=F`，约延迟 10 分钟 | 较 24 小时前 |
| 标普 500、纳斯达克 | `^GSPC` / `^IXIC`；美股休市时换成期货 `ES=F` / `NQ=F` | 较昨收；期货较美股收盘 |
| VIX、美元指数、美元/日元、比特币 | `^VIX`、`DX-Y.NYB`、`JPY=X`、`BTC-USD`（比特币不附在异动推送上） | VIX 较昨收，其余较 24 小时前 |

- 每一项的字段：`key`、`name`、`symbol`、`group`、`kind`（`yield` | `price` | `level`）、`ref`（`close` 较上一个美股收盘 | `24h`）、
  `off_hours_symbol` / `off_hours_name`（美股休市时换成的期货或 ETF，例如费城半导体 `^SOX` → `SOXX`）、`in_alert`（是否附在异动推送上）、
  `night_proxy`（收益率夜里用哪个国债期货补，默认 `auto`）。
- 开关：`macro.attach_to_alerts`、`timeout_seconds`（推送时最多等 10 秒，取不到就不附）、`cache_seconds`（60）、`treasury_curve`。
- 涨跌只用分钟线算，不用日线：期货换月时日线和分钟线是不同合约，用日线会算出假的大涨大跌。快照连同异动一起入库（`alerts.macro_json`）。
- 和某个板块有关的指标（例如光模块的美元/人民币、创业板指）写在主题的 `indicators` 里，只附在这个板块的异动上，`/macro` 里单列“板块指标”。

## 事件日历收集什么

都在 `settings.yaml` 的 `events` 里增删：

- **公司**：持仓、它们的海外主上市代码，以及生效主题里的 `peers` 和 `catalysts`（示例里就是光模块同业、英伟达、Arista、博通、云厂商……）。
  和主题无关、但也想收集的公司写在 `events.related`。来源：
  - 财报日：yfinance（全部代码，多为估计）、日本交易所集团「決算発表予定日」（东京上市，官方）、SEC 6-K（外国发行人公告的业绩发布 / 电话会时间）、Finnhub（有 key 时）；
  - 内部人拟减持：SEC Form 144（只看持仓，≥ 100 万美元，`insider_sale_min_usd`）；
  - 手工录入：`events.manual`（解禁、股东大会等）。
- **宏观**（`events.macro`，都来自官方日程，不需要 key）：

  | 配置项 | 内容 | 来源 |
  |---|---|---|
  | `fed` | `fomc` 议息决议 + 记者会（标注带点阵图的会议）、`minutes` 纪要、`chair` 主席讲话与国会证词；可加 `beige_book` | 美联储 |
  | `us_data` | `cpi` `ppi` `jobs`（非农）`jolts`：BLS，需要 `BLS_USER_AGENT`；`pce` `gdp`：BEA；`retail_sales`：人口普查局；可加 `eci`、`durable_goods` | 各机构的发布日程 |
  | `treasury_auctions` | 长端国债拍卖，默认 `[10Y, 20Y, 30Y]` | 美国财政部 |
  | `boj` | 日本央行议息 | 日本央行 |
  | `korea_exports` | 韩国月度出口与前 20 天出口 | 按惯例推算，标为“预计” |
  | `opex` | 美股月度期权到期 | 按交易所日历推算 |
  | `us_market` | 美股休市与提前收盘 | 交易所日历 |

- 每晚汇总：明天的事件 + 上次以来的新增 / 改期 / 确认 + 未来 14 天（宏观、公司分开列）。估计日期只报 45 天以内的变化（远期估计改得勤，不打扰）；
  宏观日程提前很久就排好了，所以“新增”不单独提醒，只提醒改期。
- 异动归因时，模型会看到异动前 24 小时内发生的日程（例如盘前刚公布 CPI、昨晚同业发了财报）和接下来 24 小时的日程。
- 收集未来 100 天（`events.lookahead_days`），季度财报间隔约 91 天，所以每家的下一次财报都在库里。

## 数据与存储

| 内容 | 来源 | 费用 |
|---|---|---|
| 分钟线、日线（主源、海外） | Yahoo Finance（yfinance） | 免费，非官方接口；1 分钟线只保留约 30 天 |
| 行情兜底、日线官方收盘价 | Alpaca（实时 IEX / 15 分钟以前的 SIP） | 免费账户（可选） |
| 美股夜盘（美东 20:00–04:00） | Hyperliquid 上 trade.xyz 的股票永续合约 | 免费公开接口，不要 key；只读行情，本项目不交易 |
| 美国国债收益率曲线 | 美国财政部 | 免费，官方 |
| 公司新闻、财报日历 | Finnhub | 免费 key（可选） |
| 英 / 韩 / 中新闻 | Google News RSS | 免费 |
| 美股公告、Form 144、6-K | SEC EDGAR | 免费，需要联系邮箱 |
| 财报日 | yfinance、日本交易所集团 | 免费 |
| 宏观日程 | 美联储、BLS、BEA、人口普查局、美国财政部、日本央行；期权到期、休市按交易所日历推算 | 免费 |
| 简报、归因、事件汇总、对话 | 本机 LM Studio（默认）/ Claude / OpenAI | 本地免费；云端按用量计费 |

**`data/` 里有什么**（整个目录不进 git）：

- `tickersentinel.sqlite3`：主库。异动与归因（`alerts`，也是一份事件研究数据集，`label` / `notes` 两列留给事后标注）、事件日历、简报、
  推送记录（`messages`）、对话、飞书同步的映射。
- `bars.sqlite3`：分钟线库（见下）。两个库旁边的 `-wal` / `-shm` 是 SQLite 的工作文件。
- `outbox.log`：推送渠道实际是终端时的推送内容。
- `scope_history/`：`tsen scope` 改动前的备份；`scope_candidates/`：Claude Code 准备的联动候选。

**本地分钟线库**（`storage.bars_path`）：

- 存什么：基础宏观变量和生效主题的板块指标，加上持仓、基准、同业、夜间信号。改了监控范围，新代码自动开始存。
- 怎么存：`tsen run` 每 120 秒（`bars.collect_seconds`）增量采集一次；新代码在后台补最近 29 天（`bars.backfill_days`）。
  白天用 Yahoo，美股夜盘用 Hyperliquid。财政部收益率曲线每 12 小时取一次，保留 2 年（`bars.treasury_years`）。
  超过 120 天（`bars.keep_minute_days`）的 1 分钟线每天自动压成 5 分钟线，长期保留。纯程序，不用大模型，也不需要开着网页。
- 占多大：作者的库在补了 29 天、又运行一周后约 41 MB。
- `bars.enabled: false` 关掉采集。

**断网、限流、电脑睡眠时**：单个数据源失败不会让整体停下，上层自动降级（行情换兜底、宏观快照取不到就不附、日历沿用上次的结果）。
分钟线采集里失败的代码下一轮从断掉的地方重取，恢复后自动补上；`tsen run` 停了一阵也一样，下次运行时补上空档（最多 29 天）。
休眠期间的异动无法补测，醒来后检测接着跑；错过的简报和汇总由看门狗补发。没开 `tsen run` 时数据不会自动更新，
用 `tsen bars --backfill` 或网页上的“现在补抓”。

## 长期运行

- **能连续跑很多天**：启动时把能同时打开的文件数从 macOS 默认的 256 调到 10240；每分钟的检测、分钟线采集、看门狗都有时间上限，
  某个请求挂住只会放弃那一轮；连续失败会推提醒（见「推送什么、什么时候」）。
- **更新代码后**：`git pull` 之后重启 `tsen run`（代码不会热更新；网页的静态文件带版本号，刷新页面就是新的）。
  重启时从数据库恢复冷却状态，当天推过的异动不会重推；依赖有变化时先重新 `uv pip install -e ".[dev,web,feishu]"`。
- **Mac 上开机自动运行**（可选，下面是一个 launchd 示例，路径换成你自己的；放在 `~/Library/LaunchAgents/` 下，
  用 `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/tickersentinel.plist` 加载）：

  ```xml
  <?xml version="1.0" encoding="UTF-8"?>
  <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
  <plist version="1.0">
  <dict>
    <key>Label</key><string>tickersentinel</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/bin/caffeinate</string><string>-is</string>
      <string>/path/to/tickersentinel/.venv/bin/tsen</string><string>run</string>
    </array>
    <key>WorkingDirectory</key><string>/path/to/tickersentinel</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardErrorPath</key><string>/path/to/tickersentinel/data/tsen.log</string>
  </dict>
  </plist>
  ```

- **部署到别的机器**（VPS 或家里的小主机）：那里没有本机的大模型，把 `llm.tasks` 改成 `[anthropic]` 或 `[openai]`（并填对应密钥），
  或者把 `llm.providers.local.base_url` 指向能访问到的服务并删掉 `lmstudio` 那一段。网页用 SSH 端口转发看。用 systemd 守护：

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

## 开发

```bash
uv pip install -e ".[dev,feishu,web]"   # 没装 feishu / web 时，相关的测试文件会被整个跳过
.venv/bin/pytest -q                     # 全部离线运行，不需要网络和 key
```

测试用合成的行情和假数据源，读的是 `tests/fixtures/config/`（一份冻结的配置，和示例配置互不影响）。

作者日常运行、跑测试用的组合：Python 3.12、yfinance 1.7.0、pandas 3.0、numpy 2.5、FastAPI 0.162、lark-oapi 1.7.5（macOS）。
yfinance 限定在 1.x（`>=1.7,<2`）：程序用到它的几个内部接口来区分“取数失败”和“休市”。

```
config/                 示例配置：settings.yaml、assets.yaml、themes/*.yaml
src/tickersentinel/
  cli.py                tsen 命令行
  app.py                组装所有模块；tsen run 的常驻入口（调度、监控、采集、网页、飞书）
  config.py             配置模型（settings / assets / themes / .env）；监控范围 = 持仓 × 主题
  storage.py, barstore.py   主库、分钟线库
  sources/              所有网络调用：行情、夜盘、新闻、公告、财报日、宏观日程、收益率曲线
  detect/               交易时段切段、基线（β、σ）、检测规则、同业篮子、冷却
  jobs/                 全天候检测、日程规划、分钟线采集、回放、合并推送
  events/, news/        事件日历收集与每晚汇总；新闻聚合与去重
  agents/               简报、归因、对话（含只读查询工具）、排版
  llm/                  大模型路由：LM Studio / OpenAI 兼容 / Claude，工具循环
  notify/               推送渠道：console、feishu、telegram（占位）；推送记录
  feishu/               飞书日历与多维表格同步
  scope/                tsen scope：查看、找候选、写入 / 撤销、调阈值、联动体检、飞书 /scope
  web/                  本机网页：FastAPI + Jinja2 + htmx + Lightweight Charts（随包附带，不需要 Node 构建）
tests/                  离线测试
.claude/skills/add-holding/   Claude Code 项目技能：给新持仓准备联动候选
```

设计决策、实测记录和开发约定见 [CLAUDE.md](CLAUDE.md)；还没做的功能、待核实的事项和已知问题集中在 [TODO.md](TODO.md)
（Telegram 推送、期权异动监控、韩国 DART 公告、网页上的灵敏度调节和简报存档……）。

## 许可

[MIT](LICENSE)。网页里附带的 htmx（0BSD）和 TradingView Lightweight Charts（Apache-2.0）各自的许可证在 `src/tickersentinel/web/static/vendor/`。
