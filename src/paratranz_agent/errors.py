"""跨层共用的异常类型。

只放**多个模块都要认**的异常：定义在使用方模块里会造成循环导入，
定义在 server 层则 CLI 也得反向依赖 Web。
"""
from __future__ import annotations


class BudgetExceeded(RuntimeError):
    """任务内预算检查不通过（策略 = stop）。

    为什么要单独一个类型：预算拦截既不是"模型坏了"也不是"数据有问题"，
    报表与任务终态都要能把这句话原样带给用户（"预算超了，不是翻译失败"），
    用 `RuntimeError` 混在模型错误里就只能靠字符串猜。
    """
