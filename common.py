"""语音工坊 AstrBot 插件 —— 公共定义。

这里只有纯 Python 的东西：错误类型、配置读取、音频后缀、httpx 延迟导入。
不依赖 DSH，也不依赖 AstrBot，方便单测直接 import。
"""

from __future__ import annotations

import re
from typing import Any

PLUGIN_NAME = "astrbot_plugin_voice_studio"
PLUGIN_VERSION = "0.4.4"

#: 可导入音色库的参考音频后缀（索引/GPT-SoVITS 对 wav 兼容最好，其余也可）
REF_AUDIO_SUFFIXES = {
    ".wav",
    ".mp3",
    ".flac",
    ".m4a",
    ".ogg",
    ".opus",
    ".aac",
    ".wma",
    ".aiff",
    ".aif",
}

#: /翻唱 接受的歌曲后缀
SONG_SUFFIXES = REF_AUDIO_SUFFIXES | {".mp4", ".mkv", ".webm"}

#: 音色库单个文件上限（30MB）
MAX_VOICE_BYTES = 30 * 1024 * 1024


class VoiceStudioError(RuntimeError):
    """所有可直接展示给用户的中文错误都用这个类型。"""


def load_httpx():
    """延迟导入 httpx，依赖缺失时插件仍能加载并给出中文提示。"""

    try:
        import httpx  # noqa: PLC0415 - 延迟导入是有意的
    except ImportError as exc:  # pragma: no cover - 依赖问题
        raise VoiceStudioError(
            "缺少 httpx 依赖：请在 AstrBot 插件管理里安装 requirements.txt，"
            "或手动 pip install httpx。"
        ) from exc
    return httpx


def conf_get(config: Any, key: str, default: Any = None) -> Any:
    if not config:
        return default
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001 - 配置可能是任意 dict 子类
        return default
    return default if value is None else value


def conf_str(config: Any, key: str, default: str = "") -> str:
    value = conf_get(config, key, default)
    if value is None:
        return default
    return str(value).strip()


def conf_int(config: Any, key: str, default: int = 0) -> int:
    value = conf_get(config, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def conf_float(config: Any, key: str, default: float = 0.0) -> float:
    value = conf_get(config, key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def conf_bool(config: Any, key: str, default: bool = False) -> bool:
    value = conf_get(config, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def conf_list(config: Any, key: str, default: list | None = None) -> list:
    value = conf_get(config, key, default if default is not None else [])
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def conf_obj(config: Any, key: str) -> dict:
    value = conf_get(config, key, {})
    return dict(value) if isinstance(value, dict) else {}


def format_seconds(value: float | int | None) -> str:
    try:
        seconds = max(0.0, float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def format_bytes(value: float | int | None) -> str:
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def strip_wake_prefix(text: str) -> str:
    return re.sub(r"^[/!！。]+", "", str(text or "").strip()).strip()


def unquote(token: str) -> str:
    token = str(token or "").strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token
