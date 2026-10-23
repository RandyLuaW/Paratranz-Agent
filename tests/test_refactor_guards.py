"""P2 架构重构的防回归。

这里守的不是行为，而是**结构**——因为这两条一旦破掉，坏法是静默的：
配置不落盘也返回 200、`AgentConfig` 每次调用都重建。
"""
from __future__ import annotations

from paratranz_agent.server.routes.settings import _PATCH_EXCLUDE, SettingsPatch
from paratranz_agent.server.settings import RuntimeSettings


def test_settings_patch_covers_every_patchable_field():
    """`PUT /api/settings` 的补丁模型必须覆盖 RuntimeSettings 的全部可打补丁字段。

    历史事故：新增 `writeback_*` 三项时忘了同步手抄的补丁模型，
    pydantic 把未知字段静默丢掉 —— PUT 返回 200，配置却没落盘，
    界面读回来还是旧值。现在补丁模型由 RuntimeSettings 派生，
    这条断言把「派生」这个事实钉住：谁改回手抄，测试立刻红。
    """
    expected = set(RuntimeSettings.model_fields) - _PATCH_EXCLUDE
    assert set(SettingsPatch.model_fields) == expected
    # 全部字段可选，默认 None（留空 = 不改）
    assert all(f.default is None for f in SettingsPatch.model_fields.values())
    assert len(expected) >= 20, "可打补丁的字段意外变少了"


def test_cfg_for_is_cached_but_follows_settings_change(tmp_path):
    """`cfg_for` 走缓存，但改了配置必须立刻反映。

    缓存键是设置对象本身（`SettingsStore.load()` 每次保存都换新对象），
    所以这里检查两件相反的事：同一次配置返回同一对象；保存后返回新对象。
    """
    from paratranz_agent.server.settings import SettingsStore
    from paratranz_agent.server.tools import AppContext

    store = SettingsStore(tmp_path / "settings.json")
    ctx = AppContext(store, tmp_path, mock=True)
    try:
        first = ctx.cfg_for("translate")
        assert ctx.cfg_for("translate") is first, "配置没变却重建了 AgentConfig"

        store.save({"batch_size": 17})
        second = ctx.cfg_for("translate")
        assert second is not first, "配置变过，缓存必须失效"
        assert second.translate.batch_size == 17

        # 不传 task 的默认配置与 cfg_for("") 是同一份
        assert ctx.cfg is ctx.cfg_for("")
    finally:
        ctx.close()
