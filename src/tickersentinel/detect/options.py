"""期权异动检测（第 4 阶段，尚未实现）。

零成本方案（券商 API 或 yfinance 的期权链快照）：每 poll_seconds 拉一次近月期权链，与上一次快照做差：
  - 大额成交：Δvolume × mid × 100 ≥ min_premium_usd   → “疑似大单”（快照看不到单笔，也无法区分主动买卖）
  - 新开仓迹象：当日 volume / open_interest ≥ min_volume_oi_ratio
  - 隐含波动率跳升：近月平值 IV 较开盘上升超过阈值 → 市场在为事件定价
  - put/call 成交比突变
付费方案（Massive 等的 OPRA 逐笔数据）可以识别单笔大单与成交方向（按成交价相对买卖价判断主动方）。

注意：新上市标的（如 ADR）的期权流动性可能较薄，需要单独设定阈值或暂不监控。
"""

from __future__ import annotations

import pandas as pd

from tickersentinel.config import OptionsMonitor


def diff_snapshots(prev: pd.DataFrame, curr: pd.DataFrame, cfg: OptionsMonitor) -> list[dict]:
    """比较两次期权链快照，返回异动合约列表。"""
    raise NotImplementedError("期权监控在第 4 阶段实现，见本文件说明与 CLAUDE.md 路线图")
