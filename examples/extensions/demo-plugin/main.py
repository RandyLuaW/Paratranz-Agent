"""示例插件入口（占位）。

接入插件运行引擎后，这里应实现 register(ctx) 之类的入口，
把命令、钩子、WebUI 页面注册到宿主。
"""


def register(ctx):  # pragma: no cover - 运行引擎尚未接入
    raise NotImplementedError("插件运行引擎待接入；当前仅做登记与启停")
