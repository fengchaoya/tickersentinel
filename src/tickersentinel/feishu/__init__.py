"""飞书日历与多维表格同步（推送 / 收消息见 notify/feishu.py）。

- client.py         开放平台接口的薄封装（lark-oapi 类型化请求）+ 错误码 → 中文处理建议
- calendar_sync.py  事件日历（events 表）→ 应用自建的共享日历，你是日历成员
- bitable_sync.py   异动 + 归因（alerts 表）→ 多维表格的一行；你填的 label / notes 拉回 alerts 表
- sync.py           FeishuSync：App 与命令行共用的入口（找 open_id、失败退避、错误提醒）

开关在 settings.yaml 的 notify.feishu_sync（默认关闭）；一次性设置：tsen feishu calendar-setup / bitable-setup。
SDK（lark-oapi）是可选依赖：这里的模块在调用接口时才导入它。
"""

from tickersentinel.feishu.client import FeishuApi, FeishuError
from tickersentinel.feishu.sync import FeishuSync

__all__ = ["FeishuApi", "FeishuError", "FeishuSync"]
