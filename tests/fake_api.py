"""测试用的假平台：实现已提升为包内模块 `paratranz_agent.fake`，此处仅转发。"""
from paratranz_agent.fake import FakeParaTranz, _seed  # noqa: F401

__all__ = ["FakeParaTranz", "_seed"]
