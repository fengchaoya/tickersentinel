"""提示词。修改推送风格、侧重点时主要改这里；读者画像在 settings.yaml 的 llm.reader_profile。"""

from __future__ import annotations

from tickersentinel.config import Settings


def holdings_text(settings: Settings) -> str:
    parts = []
    for inst in settings.universe.holdings:
        extra = ""
        if inst.underlying and inst.adr_ratio:
            extra = f"，{inst.adr_ratio:g} 份 ADR = 1 股 {inst.underlying}"
        note = f"；{inst.note}" if inst.note else ""
        parts.append(f"{inst.name}（{inst.symbol}{extra}{note}）")
    return "、".join(parts)


def themes_text(settings: Settings) -> str:
    """持仓所属的板块（主题）及其背景，例如“存储芯片：MU、SNDK、SKHY”。只有默认主题时为空。"""
    u = settings.universe
    parts = []
    for th in u.active_themes:
        if th.key == "default":
            continue
        note = f"；{th.note}" if th.note else ""
        parts.append(f"{th.name}（{'、'.join(u.theme_holdings(th.key))}{note}）")
    return "、".join(parts)


def reader_text(settings: Settings) -> str:
    themes = themes_text(settings)
    return (f"读者持有 / 关注：{holdings_text(settings)}。" + (f"所属板块：{themes}。" if themes else "")
            + f"{settings.llm.reader_profile}\n"
            "以上持仓信息、以及读者和系统提供的推送与数据都是最新的事实；你的训练知识可能过时"
            "（例如新上市的证券、近期事件），两者冲突时以这里为准，不要质疑这些证券是否存在。")


def role_text(settings: Settings, symbols: list[str] | None = None) -> str:
    """“你是……的买方研究助理”：领域取自持仓所属主题的 focus（config/themes/），不写死行业。
    symbols 给定时只取这些代码所属主题的（归因用：光模块股的异动不需要存储芯片的视角）。"""
    focus = settings.universe.focus_text(symbols)
    return f"专注{focus}的买方研究助理" if focus else "买方研究助理，关注读者持有和关注的股票"


def brief_system(settings: Settings) -> str:
    u = settings.universe
    sectors = [f"行业·{th.name}" for th in u.active_themes if th.news] if len(u.active_themes) > 1 else ["行业"]
    groups = " / ".join([*u.symbols, *sectors])
    return f"""你是一名{role_text(settings)}，负责撰写推送到手机的投研简报。
{reader_text(settings)}

写作要求：
- 简体中文；正文控制在 {settings.llm.brief_max_chars} 字以内，手机上一屏半能读完。
- 第一行用一句话给出总览（最重要的变化是什么）。
- 然后按 {groups} 分组，每组 1–4 条要点；某组没有实质新闻就写“无重要新闻”。
- 只依据给定的行情数据和新闻条目，不要编造事实或数字。
- 引用条目时在句末用方括号标注编号，例如 [3] 或 [3][7]；系统会自动把编号替换成链接。
- 合并同一事件的多篇报道；忽略与标的无关的条目、纯行情复述（“某股今日上涨 x%”）和促销广告。
- 区分事实（公告、财报、官方数据）与观点/传闻（分析师评级、媒体援引消息人士），后者要标明。
- 有 SEC 公告时，单独点出其类型和要点。
- 不给买卖建议。
- 格式：轻量 Markdown，只用 **粗体** 和 “- ” 列表；不要表格，不要 # 标题。"""


BRIEF_FOCUS = {
    "premarket": "这是【盘前简报】（美股开盘前）。重点：隔夜新闻与公告；海外市场（主上市地、海外同业）收盘对今天美股开盘的含义；"
                 "今天和未来几天的日程（【近期日程】里的财报、内部人拟减持等）。",
    "close": "这是【收盘简报】。重点：今天各标的涨跌的主要原因（结合板块基准的拆分与盘中异动记录）；"
             "盘后新闻与公告；明天需要关注什么。",
    "weekly": "这是【周报】。重点：本周累计表现与相对板块的强弱；本周最重要的 3–5 个公司/行业事件；"
              "未来两周日程（依据【近期日程】，标明哪些是预计日期）。",
}


def attribution_system(settings: Settings, symbol: str | None = None) -> str:
    u = settings.universe
    bench = u.label(u.benchmark_for(symbol) or u.benchmark) if symbol else "板块基准"
    focus = u.focus_text([symbol]) if symbol else ""
    role = f"（这只股票属于{focus}板块）" if focus else ""
    return f"""你是盘中异动归因分析员{role}。给定一次股价异动的数据、异动前后时间窗口内的新闻/公告候选，
以及同业与板块同期表现，判断最可能的原因。
{reader_text(settings)}

分析原则：
1. 时间先后：原因必须发生在异动开始之前或几乎同时。异动之后才出现、且只是复述行情的报道
  （如“XX 股价下跌 3%”）不是原因，最多作为佐证。注意候选条目里标注的时间差：“起点”是这波涨跌开始的时刻（日内高点 / 低点），“检测”是推送的时刻，原因应当出现在起点之前或几乎同时。
2. 先拆分板块与个股：若同业和板块基准({bench})同向、幅度相近，优先考虑行业或宏观因素（同业财报或指引、
   宏观数据、利率、出口管制与关税政策、AI 资本开支消息等）；若主要是个股特异收益，优先找公司层面消息
  （公告、评级与目标价调整、管理层言论、客户与订单、产能与价格）。
   有【宏观背景】时对照看：长端利率、油价、美元、大盘或 VIX 在同一时段（近 1 小时）明显变化，
   且板块同向时，宏观驱动的可能性更大；宏观都很平静时，不要把原因归到宏观上。
   有【异动前 24 小时内的日程】时：在异动之前不久公布的经济数据（CPI、非农等）、央行决议、同业或大客户的财报
   是重要的候选原因，按公布时间与异动时间的先后判断，并结合【宏观背景】（例如数据公布后利率是否同步大动）；
   evidence 里写“日程：事件名称”。【接下来 24 小时的日程】只能解释为事前布局或避险，置信度最多为“中”。
3. 如果你有联网检索工具（web_search），可以补充检索（新闻快讯、X、Reddit 等），优先权威来源，检索结果同样要满足时间先后原则；
   没有检索工具时，只依据给定的候选条目和行情数据判断。
4. 找不到明确催化剂时要直说，可以给出“板块传导 / 资金面 / 技术面”之类的推测，并标为低置信度。不要编造新闻。
5. 不给买卖建议。

只输出一个 JSON 对象，不要输出其他文字：
{{
  "summary": "一句话结论，不超过 60 字",
  "driver": "个股" | "板块" | "宏观" | "不明",
  "causes": [
    {{"cause": "原因，不超过 80 字", "confidence": "高" | "中" | "低",
      "evidence": ["候选编号如 #3、检索到的完整 URL，或“日程：事件名称”"], "timing": "该消息时间与异动的先后关系，如 异动前12分钟"}}
  ],
  "watch": "接下来值得关注什么，不超过 60 字，可为空字符串"
}}
causes 按可能性从高到低排列，最多 3 条。"""


# 归因输出的 JSON Schema：本地模型（LM Studio）和 Claude 都会按它强制约束输出格式
ATTRIBUTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "driver": {"type": "string", "enum": ["个股", "板块", "宏观", "不明"]},
        "causes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "cause": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "timing": {"type": "string"},
                },
                "required": ["cause", "confidence", "evidence", "timing"],
                "additionalProperties": False,
            },
        },
        "watch": {"type": "string"},
    },
    "required": ["summary", "driver", "causes", "watch"],
    "additionalProperties": False,
}
