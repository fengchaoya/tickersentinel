"""本机网页（tsen run 顺带启动 / tsen web 单独打开）：总览、异动、事件日历、宏观。

结构：
- context.py   WebContext：网页要用的东西（配置、数据库、行情、事件、宏观……），由 App.web_context() 组装，测试可以直接构造
- factory.py   create_app(ctx) → FastAPI；挂上安全检查、静态文件、各页面的路由
- security.py  只限本机：Host 白名单（防 DNS 重绑定）、写请求要带页面里的 token（防其他网站偷偷调用）
- common.py    模板、过滤器（时间、涨跌、Markdown）、运行状态
- server.py    嵌入 tsen run 的 uvicorn（不接管信号）、单独运行的 tsen web、探测已经在跑的网页
- pages/       各页面的路由（overview 总览、alerts 异动、calendar 日历、macro 宏观、chat 网页提问），模板在 templates/<页面>/，
               样式和脚本在 static/；各页的数据在 overview_feed / overview_panels / alerts_data / alerts_chart / calendar_view / macro_data
依赖是可选的（pip install -e ".[web]"）：没装时 available() 为 False，tsen run 跳过网页、照常运行。
"""

from __future__ import annotations

import importlib.util

WEB_DEPS = ("fastapi", "uvicorn", "jinja2")


def available() -> bool:
    return all(importlib.util.find_spec(m) is not None for m in WEB_DEPS)


def missing_deps() -> list[str]:
    return [m for m in WEB_DEPS if importlib.util.find_spec(m) is None]
