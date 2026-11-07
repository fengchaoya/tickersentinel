"""监控范围：查看（view）、发现联动并给出方案（discover）、确认后写入配置并可撤销（edit）。

监控范围 = 持仓（config/assets.yaml）× 它们的主题（config/themes/）；基础宏观变量与宏观日程在 settings.yaml，和具体股票无关。
"""

from tickersentinel.scope.view import describe_scope

__all__ = ["describe_scope"]
