# TODO — 以后接着做的改动

> 这里集中记录还没做的功能、还没核实的事项、已知没修的问题，以及发布前审计里有意没改的项。
> 已经做完的设计和它们的理由写在 [CLAUDE.md](CLAUDE.md)（「关键设计决策」「当前进度」）；做完一项就从这里删掉，把结论写进 CLAUDE.md。
> 原则（作者 2026-11-06）：现在的版本已经能稳定长期运行，改动以“不改现有功能”为前提。

## 下一步功能

1. **推送渠道：Telegram**（`notify/telegram.py`，文件开头有实现计划）。作者现在用飞书、一切正常，再加一个 IM 不是优先事项，暂缓。
   做的时候复用同一个 `ChatAgent`（双向对话），长轮询、不需要公网地址（决策 8）。
2. **飞书同步的收尾**（决策 14）：
   - `tsen check` 用只读 GET 探测日历 / 多维表格的权限（现在要用 `tsen feishu sync` 实测）；
   - `tsen feishu sync` 与 `tsen run` 同时写同一张表时，锁只在进程内：考虑加文件锁，或者至少给出警告。
3. **期权异动监控**（第 4 阶段，`detect/options.py`）：先做零成本的快照差分版（MU / SNDK / SKHY 的期权都已上市且有流动性，
   见 CLAUDE.md「联网核实结论」第 3 条）。现在只有占位：`diff_snapshots` 抛 `NotImplementedError`，`sources/base.py` 的 `OptionsSource`
   没有实现，`monitor.options.enabled: false`。
4. **事件日历扩展**：
   - 韩国 DART（SK海力士的大量持股 / 高管与主要股东交易计划报告 D005），需要申请 `OPENDART_API_KEY`（`.env.example` 里是占位）；
   - 424B7 / S-3ASR 转售与锁定期条款自动提取；
   - SKHY 的 ADS 存托上限变化。
5. **本机网页**（决策 20）：还没做的两页——**灵敏度调节**（在网页上做 `tsen scope tune` 的事：按波动给建议、回放对比、确认写入）、
   **简报存档**（按日期翻看历次简报）。
6. **检测**：
   - σ 的日内画像：开盘后半小时的波动是全天均值的两三倍（U 形），σ_1m 用全天的 MAD，开盘时 5 分钟档偏灵敏（决策 2「已知可以改进的」）。
     用户 10-24 说开盘照常报告，暂不做。
   - 铠侠（285A.T）阈值要不要放宽：东京时段起伏极大，10 天回放 26 条；用户 10-24 决定先维持现状。
   - 美股周末（周五 20:00–周日 20:00）：在 `UsEquityClock` 加 kind="weekend" 的段，`hours` 加 24x7（决策 16）。周末无法交易，暂不盯。
7. **其他**：
   - 归因准确率评估（利用 alerts 表的 `label` / `notes`，决策 7）；
   - 简报里加入成交量和资金流指标；
   - 总览的宏观看板可以改成读分钟线库（现在每个打开的页面每 60 秒各自取一次 Yahoo，见下面「发布前审计未改的项」）。

## 待核实

- **Finnhub 是否覆盖 SKHY**（缺 key）：文档写 company-news “只覆盖北美公司”，没说美国上市的外国 ADR 算不算；财报日历可能只挂在
  `000660.KS` 下（需要 `international=true`）。不覆盖时表现为 0 条而不是报错。端点、参数、字段已对照官方 swagger.json，
  无凭证请求返回 401 并转成可读的 SourceError。拿到 key 后先跑 `tsen check`。
- **Claude / OpenAI**（缺 key）：`claude-sonnet-5-5` + `web_search_20260209` + `fallbacks: "default"`，以及 OpenAI `gpt-5-mini` 的实际调用
  （`tsen check` 会对 `llm.tasks` 用到的每个供应商做一次连通测试）。Claude 的工具循环只用假的 SDK 客户端（`tests/test_llm.py` 的
  `FakeAnthropic`）测过，请求形状没经过真实 API 校验；`claude_research`（`llm.tasks.scope` 的备用档）也还没真实调用过。
- **Alpaca**：2026-10-23 已用免费 key 实测（决策 18）。还没覆盖到的：IEX / SIP 日线没有复权（`adjustment=raw`），只在兜底时才会用到
  它的整份日线，遇到拆股要留意。
- **飞书日历 / 多维表格**（不需要新 key，需要开通 `calendar:calendar`、`bitable:app`）：只用 `tests/test_feishu_sync.py` 的 FakeFeishu
  离线测过，请求字段、枚举、响应键已对照 lark-oapi 1.7.5 源码。上线步骤见 CLAUDE.md「当前进度」。重点看：全天日程的结束日期是否确实
  不含当天（目前依据 larksuite/cli 的处理推断）；订阅后日程详情是否可见；batch_create 之后马上检索能否检到新行（索引有延迟的话会被
  误判为已删除）；idempotency_key / client_token 的去重时效（文档没写）。
- **飞书 `/scope add`**：还没在真实飞书里点过（`tsen ask "/scope add LITE"` 走的是同一套逻辑）。
- **BLS 发布日程**：只用占位邮箱测过解析（`.env` 的 `BLS_USER_AGENT`）。
- **示例持仓 LITE 的夜盘**：示例配置的美股默认 `hours: 24x5`，但夜盘数据只有 trade.xyz 上架的代码才有；LITE 在不在上面没核实过
  （发布前审计是离线做的）。不在的话，新用户第一次运行夜里没有数据，也没有明显的提示——用 `tsen check` 看一眼；不在就给示例的
  LITE 写 `hours: extended`，或在 assets.yaml 里加一句注释。

## 已知问题（没修）

- 报价的时段标签不认提前收盘日（11-27 半日市 13:00–16:00 ET 的分钟线仍标为 regular）。
- 海外代码（.KS）在首尔交易时段也显示 closed（只有日线）。
- 飞书日历 `invite_user` 从 true 改成 false 不会移除已加的参与人（决策 14）。
- Yahoo 的首尔、东京分钟线标明延迟 20 分钟，这两个市场的异动推送天然晚约 20 分钟。
- `.env` 没填 `BLS_USER_AGENT` 时日历里没有 CPI / 非农 / PPI，对话会据此答“下周没有 CPI”（2026-11-01 实测）：要提醒用户填上。
- 降级简报（没有大模型时）只显示前 5 条，却把选中的条目全部标记为已读。
- Yahoo 连续合约（CL=F、GC=F、ZN=F）换月时分钟线会跳一下，宏观页的图上不调整（ZN=F 换月那一晚的折算收益率也会跳，决策 21）。
- 只开 `tsen web`、没开 `tsen run` 时分钟线不自动更新（宏观页能“现在补抓”）。

## 发布前审计未改的项（2026-11-06）

这次审计按“维持功能、只改陈述”处理：文档、注释、帮助文字、元数据和死代码已经改了；下面这些会改变行为或需要另外决定，先记在这里。

- **Telegram 可以选，但选了推送时才报错**：`notify.provider: telegram` 且填了 `TELEGRAM_*` 时，`TelegramNotifier` 能构造出来，
  `start` / `send` 抛 `NotImplementedError`，每条推送都失败、进补发循环（没填密钥时才退回 console）。`pyproject.toml` 的 `telegram`
  extra 和 `.env.example` 的 `TELEGRAM_*` 都是占位。可以让 `__init__` 直接抛 `RuntimeError`（`build_notifier` 会警告并退回 console），
  等真正实现时再改回来；作者说飞书可用、另一个 IM 不是优先事项，先不动。
- **`OPENDART_API_KEY` 是占位**：DART 还没实现（见上面「下一步功能」第 4 条），`.env.example` 和 README 都标了“还没实现”。
- **`detect/options.py` 是占位**：没有任何模块导入它（见上面第 3 条）；README 写了“还没做”。
- **网页图标只有 SVG**：`manifest.webmanifest` 和 `apple-touch-icon` 都指向 `icon.svg`。Safari 不认 SVG 的 apple-touch-icon，“添加到程序坞”
  可能显示一个字母方块；有的 Chrome 版本要 192 / 512 的 PNG 才弹完整的安装提示（“将页面作为应用安装”照样能用）。没在浏览器里核实过。
  可以从 icon.svg 生成 180 / 192 / 512 的 PNG。
- **总览宏观看板每个页面各自取 Yahoo**：`/partials/overview/macro` 每 60 秒刷新、调 `ctx.macro.snapshot()`（yfinance，60 秒缓存），
  和分钟线采集是分开的；开几个页面，再加一个单独运行的 `tsen web`（自己的缓存），请求就成倍增加。现在的规模无害，考虑过去遇到过的
  Yahoo 限流，记一笔（可以改成 `tsen run` 在采集时从分钟线库取）。
- **`--config` 和 `TICKERSENTINEL_SETTINGS` 的相对路径基准不同**：`--config` 按当前目录解析，环境变量按项目根目录解析（`config_paths`）。
  在别的目录下执行 `tsen --config config.local/settings.yaml` 会找不到文件。README 已经写明了；要统一的话可以让 `--config` 先按当前目录
  找、找不到再按项目根目录。
- **非 editable 安装**：`PROJECT_ROOT` 是包所在位置的上两级目录（或 `TICKERSENTINEL_HOME`），普通 `pip install .` 后会指进 Python 的库目录，
  报“找不到配置文件”，`data/` 也会建在那里。README 已写明“在克隆的目录里用 -e 安装”和 `TICKERSENTINEL_HOME`；可以在 PROJECT_ROOT 下面
  没有 `config/` 时给一句更明确的提示。
- **示例持仓 LITE 继承 24x5，trade.xyz 未必有它**：见上面「待核实」最后一条。
- **CLAUDE.md 很长、全是中文**：约 110 KB 的开发记录（决策、实测、对话里定下的事，含开发机配置）。对用 Claude Code 的贡献者有用，
  第一次来的访客可能把它当主文档——README 里已注明它是开发笔记。开发机配置要不要留由作者决定。
- **pytest 一律忽略 DeprecationWarning**（`pyproject.toml` 的 `filterwarnings`）：依赖升级前的弃用提示看不到。可以只忽略已知的几条。
- **心跳里的 `next_jobs`**（`App.write_heartbeat`，`app.job_label`）原来只给总览的运行状态方块用；方块删掉后没人读它，
  为了不改心跳格式先留着，以后可以去掉。
- **网页里没有免责声明**：去掉页脚后，“不构成投资建议”只在 README 里。公开发布时可以在不显眼的地方（启动消息、宏观页的数据来源行）加一句。
