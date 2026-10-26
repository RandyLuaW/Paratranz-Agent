"""首次启动的免费声明：后端只记「看过没有」，文案与仓库地址在这里给一份。

为什么由后端记而不是浏览器 localStorage：用户说的「首次启动」是**按这台机器上的
这次安装**算的。localStorage 按浏览器隔离，换个浏览器就又弹一次，而重装系统/清缓存
反而会把「已经看过」忘掉 —— 与「首次启动」的本意正好相反。
状态落一个独立的小文件（**不塞进 settings.json**：那是用户配置，会经派生补丁模型
出现在 API 里，加一个只读标记会污染配置面）。
"""
from __future__ import annotations

import json
from pathlib import Path

REPO = "https://github.com/RandyLuaW/Paratranz-Agent"
NOTICE_FILE = "NOTICE.txt"

# 中英双语**同时**给出：弹出窗口不跟随界面语言，避免英文界面的用户看不到中文说明、
# 或中文用户看不到英文原文（这类"免费声明"要让两种读者都能读到原文）。
TEXT_ZH = (
    "本软件完全免费，不以任何形式收费。唯一官方渠道是 GitHub 仓库；"
    "若您通过任何付费渠道取得本软件，请立即申请退款，并到官方仓库免费获取软件本体。"
)
TEXT_EN = (
    "This software is completely free of charge. The only official channel is the "
    "GitHub repository. If you obtained it through any paid channel, please request a "
    "refund immediately and download it from the official repository at no cost."
)

_ACK_FILE = "notice-free-ack.json"


def _ack_path(state_dir: Path) -> Path:
    return Path(state_dir) / _ACK_FILE


def is_acknowledged(state_dir: Path) -> bool:
    """这台机器上是否已经确认过（读不到/坏文件一律当作"没确认过"）。"""
    path = _ack_path(state_dir)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(isinstance(data, dict) and data.get("acknowledged"))


def acknowledge(state_dir: Path) -> None:
    """记下"已确认"。写失败不该让界面报错——下次再弹一次比挡住用户强。"""
    path = _ack_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"acknowledged": True}, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass


def payload(state_dir: Path) -> dict:
    """给前端的完整声明：是否需要弹、双语正文、官方仓库、随包文本文件名。"""
    return {
        "pending": not is_acknowledged(state_dir),
        "text_zh": TEXT_ZH,
        "text_en": TEXT_EN,
        "repo": REPO,
        "notice_file": NOTICE_FILE,
    }
