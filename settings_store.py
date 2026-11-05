"""插件自己的设置存储（data/settings.json）。

AstrBot 的 `_conf_schema.json` 无法做到「选哪个提供商只显示哪个」，也没有音色上传 UI，
所以 v0.3.0 起由插件可视化视图管理这些字段，并写入插件自己的 `data/settings.json`：

    读取顺序（高 → 低）：data/settings.json > AstrBot 插件配置

`_conf_schema.json` 里被页面接管的字段标记为 ``invisible``，但仍然是合法的向后兼容回退值。

本模块不依赖 AstrBot，方便单测直接 import。
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from .common import VoiceStudioError, conf_bool, conf_float, conf_int, conf_obj, conf_str
from .tts_providers import PROVIDERS

SETTINGS_VERSION = 1

_PROVIDER_KEYS: dict[str, dict[str, type]] = {
    "indextts": {"baseUrl": str, "lang": str, "emoWeight": float, "durationFactor": float},
    "gptsovits": {
        "baseUrl": str,
        "textLang": str,
        "promptText": str,
        "promptLang": str,
        "speed": float,
    },
    "siliconflow": {"baseUrl": str, "apiKey": str, "model": str, "voice": str, "format": str},
    "minimax": {
        "baseUrl": str,
        "apiKey": str,
        "groupId": str,
        "model": str,
        "voice": str,
        "format": str,
    },
    "mimo": {
        "baseUrl": str,
        "apiKey": str,
        "model": str,
        "voice": str,
        "style": str,
        "format": str,
    },
    "openai": {
        "baseUrl": str,
        "apiKey": str,
        "model": str,
        "voice": str,
        "format": str,
        "emotionInstructions": bool,
    },
}

_WORKER_KEYS: dict[str, type] = {
    "url": str,
    "token": str,
    "model": str,
    "index": str,
    "f0Method": str,
    "keyShift": int,
}

_F0_METHODS = {"rmvpe", "pm", "harvest", "crepe", "fcpe"}
_FORMATS = {"wav", "mp3", "opus", "flac", "aac", "pcm"}

_MAX_LEN = {
    "baseUrl": 500,
    "apiKey": 500,
    "model": 200,
    "voice": 200,
    "promptText": 2000,
    "style": 500,
    "url": 500,
    "token": 500,
    "index": 1000,
    "defaultVoice": 200,
}


def _clean_str(value: object, key: str) -> str:
    text = str(value if value is not None else "").strip()
    limit = _MAX_LEN.get(key, 500)
    if len(text) > limit:
        raise VoiceStudioError(f"字段 {key} 太长（最多 {limit} 字符）")
    return text


def _clean_provider_config(provider: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise VoiceStudioError(f"提供商 {provider} 的配置必须是对象")
    allowed = _PROVIDER_KEYS[provider]
    cleaned: dict = {}
    for key, kind in allowed.items():
        if key not in raw:
            continue
        value = raw.get(key)
        if kind is bool:
            cleaned[key] = bool(value)
            continue
        if kind is float:
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise VoiceStudioError(f"{provider}.{key} 必须是数字") from exc
            if key == "speed":
                number = max(0.5, min(2.0, number))
            elif key == "emoWeight":
                number = max(0.0, min(1.0, number))
            elif key == "durationFactor":
                number = max(0.5, min(2.0, number))
            cleaned[key] = number
            continue
        cleaned[key] = _clean_str(value, key)
        if key == "format" and cleaned[key]:
            if cleaned[key].lower() not in _FORMATS:
                raise VoiceStudioError(f"不支持的输出格式：{cleaned[key]}")
            cleaned[key] = cleaned[key].lower()
    return cleaned


def validate_settings_patch(payload: object) -> dict:
    """校验页面提交的设置补丁，返回只含受管字段的规范化 dict。

    支持部分字段（例如「设为默认」只提交 ``{"defaultVoice": "v123"}``）。
    未知提供商 / 非法类型会抛中文 :class:`VoiceStudioError`。
    """

    if isinstance(payload, dict) and "settings" in payload:
        payload = payload.get("settings")
    if not isinstance(payload, dict):
        raise VoiceStudioError("设置内容必须是 JSON 对象")

    patch: dict = {}
    if "ttsProvider" in payload:
        provider = str(payload.get("ttsProvider") or "").strip()
        if provider not in PROVIDERS:
            raise VoiceStudioError(
                f"未知的 TTS 提供商「{provider}」，可选：" + " / ".join(PROVIDERS)
            )
        patch["ttsProvider"] = provider

    if "providers" in payload:
        providers = payload.get("providers")
        if not isinstance(providers, dict):
            raise VoiceStudioError("providers 必须是对象")
        cleaned_providers: dict = {}
        for provider, raw in providers.items():
            if provider not in PROVIDERS:
                raise VoiceStudioError(f"未知的 TTS 提供商「{provider}」")
            if not isinstance(raw, dict):
                raise VoiceStudioError(f"提供商 {provider} 的配置必须是对象")
            cleaned = _clean_provider_config(provider, raw)
            if cleaned:
                cleaned_providers[provider] = cleaned
        if cleaned_providers:
            patch["providers"] = cleaned_providers

    if "worker" in payload:
        worker_raw = payload.get("worker")
        if not isinstance(worker_raw, dict):
            raise VoiceStudioError("worker 必须是对象")
        cleaned_worker: dict = {}
        for key, kind in _WORKER_KEYS.items():
            if key not in worker_raw:
                continue
            value = worker_raw.get(key)
            if kind is int:
                try:
                    number = int(value)
                except (TypeError, ValueError) as exc:
                    raise VoiceStudioError(f"worker.{key} 必须是整数") from exc
                cleaned_worker[key] = max(-24, min(24, number))
                continue
            cleaned_worker[key] = _clean_str(value, key)
        if "f0Method" in cleaned_worker and cleaned_worker["f0Method"]:
            if cleaned_worker["f0Method"] not in _F0_METHODS:
                raise VoiceStudioError(
                    f"worker.f0Method 不支持：{cleaned_worker['f0Method']}，可选："
                    + " / ".join(sorted(_F0_METHODS))
                )
        if cleaned_worker:
            patch["worker"] = cleaned_worker

    if "defaultVoice" in payload:
        patch["defaultVoice"] = _clean_str(payload.get("defaultVoice"), "defaultVoice")
    if "defaultEmotion" in payload:
        patch["defaultEmotion"] = _clean_str(payload.get("defaultEmotion"), "defaultVoice")
    if "emotionIntensity" in payload:
        try:
            intensity = float(payload.get("emotionIntensity"))
        except (TypeError, ValueError) as exc:
            raise VoiceStudioError("emotionIntensity 必须是数字") from exc
        patch["emotionIntensity"] = max(0.0, min(1.0, intensity))
    if "ttsSpeed" in payload:
        try:
            speed = float(payload.get("ttsSpeed"))
        except (TypeError, ValueError) as exc:
            raise VoiceStudioError("ttsSpeed 必须是数字") from exc
        patch["ttsSpeed"] = max(0.5, min(2.0, speed))
    if "randomVoiceEnabled" in payload:
        value = payload.get("randomVoiceEnabled")
        if isinstance(value, bool):
            patch["randomVoiceEnabled"] = value
        elif isinstance(value, str):
            patch["randomVoiceEnabled"] = value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        else:
            patch["randomVoiceEnabled"] = bool(value)
    if "randomVoiceProbability" in payload:
        try:
            probability = float(payload.get("randomVoiceProbability"))
        except (TypeError, ValueError) as exc:
            raise VoiceStudioError("randomVoiceProbability 必须是数字") from exc
        patch["randomVoiceProbability"] = max(0.0, min(1.0, probability))
    if "randomVoiceMaxChars" in payload:
        try:
            max_chars = int(payload.get("randomVoiceMaxChars"))
        except (TypeError, ValueError) as exc:
            raise VoiceStudioError("randomVoiceMaxChars 必须是整数") from exc
        patch["randomVoiceMaxChars"] = max(10, min(500, max_chars))
    if "randomVoiceTakeover" in payload:
        value = payload.get("randomVoiceTakeover")
        if isinstance(value, bool):
            patch["randomVoiceTakeover"] = value
        elif isinstance(value, str):
            patch["randomVoiceTakeover"] = value.strip().lower() in {"1", "true", "yes", "on", "开启"}
        else:
            patch["randomVoiceTakeover"] = bool(value)
    return patch


def _deep_patch(base: dict, patch: dict) -> dict:
    result = dict(base)
    for key, value in patch.items():
        if key in {"providers", "worker"} and isinstance(value, dict):
            current = result.get(key) if isinstance(result.get(key), dict) else {}
            merged = {item: dict(cfg) if isinstance(cfg, dict) else cfg for item, cfg in current.items()}
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    existing = merged.get(sub_key)
                    existing = dict(existing) if isinstance(existing, dict) else {}
                    existing.update(sub_value)
                    merged[sub_key] = existing
                else:
                    merged[sub_key] = sub_value
            result[key] = merged
        else:
            result[key] = value
    return result


def merge_settings(stored: dict | None, astrbot_config: object) -> dict:
    """把 data/settings.json 叠加到 AstrBot 配置上（settings 优先）。"""

    stored = stored if isinstance(stored, dict) else {}
    config = astrbot_config if isinstance(astrbot_config, dict) else {}

    stored_provider = str(stored.get("ttsProvider") or "").strip()
    config_provider = str(config.get("ttsProvider") or "").strip()
    provider = stored_provider or config_provider or "indextts"
    if provider not in PROVIDERS:
        provider = "indextts"

    stored_providers = stored.get("providers") if isinstance(stored.get("providers"), dict) else {}
    providers: dict[str, dict] = {}
    for provider_id in PROVIDERS:
        base = conf_obj(config, provider_id)
        override = stored_providers.get(provider_id) if isinstance(stored_providers.get(provider_id), dict) else {}
        merged = dict(base)
        merged.update(override)
        providers[provider_id] = merged

    base_worker = {
        "url": conf_str(config, "workerUrl", ""),
        "token": conf_str(config, "workerToken", ""),
        "model": conf_str(config, "workerModel", ""),
        "index": conf_str(config, "workerIndex", ""),
        "f0Method": conf_str(config, "workerF0Method", "rmvpe") or "rmvpe",
        "keyShift": conf_int(config, "workerKeyShift", 0),
    }
    stored_worker = stored.get("worker") if isinstance(stored.get("worker"), dict) else {}
    base_worker.update({key: value for key, value in stored_worker.items() if value is not None})

    default_voice = (
        str(stored.get("defaultVoice"))
        if "defaultVoice" in stored
        else conf_str(config, "defaultVoice", "")
    )
    default_emotion = (
        str(stored.get("defaultEmotion"))
        if "defaultEmotion" in stored
        else conf_str(config, "defaultEmotion", "auto")
    )

    def _number(key: str, stored_value: object, fallback: float, low: float, high: float) -> float:
        value = stored_value if stored_value is not None else fallback
        try:
            return max(low, min(high, float(value)))
        except (TypeError, ValueError):
            return fallback

    emotion_intensity = _number(
        "emotionIntensity",
        stored.get("emotionIntensity") if "emotionIntensity" in stored else None,
        conf_float(config, "emotionIntensity", 0.6),
        0.0,
        1.0,
    )
    tts_speed = _number(
        "ttsSpeed",
        stored.get("ttsSpeed") if "ttsSpeed" in stored else None,
        conf_float(config, "ttsSpeed", 1.0),
        0.5,
        2.0,
    )
    random_voice_enabled = (
        bool(stored.get("randomVoiceEnabled"))
        if "randomVoiceEnabled" in stored
        else conf_bool(config, "randomVoiceEnabled", False)
    )
    random_voice_probability = _number(
        "randomVoiceProbability",
        stored.get("randomVoiceProbability") if "randomVoiceProbability" in stored else None,
        conf_float(config, "randomVoiceProbability", 0.15),
        0.0,
        1.0,
    )
    try:
        random_voice_max_chars = int(
            stored.get("randomVoiceMaxChars")
            if "randomVoiceMaxChars" in stored
            else conf_int(config, "randomVoiceMaxChars", 80)
        )
    except (TypeError, ValueError):
        random_voice_max_chars = 80
    random_voice_max_chars = max(10, min(500, random_voice_max_chars))
    random_voice_takeover = (
        bool(stored.get("randomVoiceTakeover"))
        if "randomVoiceTakeover" in stored
        else conf_bool(config, "randomVoiceTakeover", True)
    )

    return {
        "ttsProvider": provider,
        "providers": providers,
        "worker": base_worker,
        "defaultVoice": str(default_voice or ""),
        "defaultEmotion": str(default_emotion or "auto"),
        "emotionIntensity": max(0.0, min(1.0, emotion_intensity)),
        "ttsSpeed": max(0.5, min(2.0, tts_speed)),
        "randomVoiceEnabled": random_voice_enabled,
        "randomVoiceProbability": max(0.0, min(1.0, random_voice_probability)),
        "randomVoiceMaxChars": random_voice_max_chars,
        "randomVoiceTakeover": random_voice_takeover,
    }


class SettingsStore:
    """data/settings.json 的读写（原子写 + 线程锁）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def read(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceStudioError(
                f"设置文件损坏：{self.path}（{exc}）。删除该文件后会回退到 AstrBot 插件配置。"
            ) from exc
        if not isinstance(data, dict):
            raise VoiceStudioError(f"设置文件格式错误：{self.path}")
        return data

    def write(self, data: dict) -> dict:
        payload = dict(data)
        payload.setdefault("version", SETTINGS_VERSION)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise VoiceStudioError(f"写入设置文件失败：{exc}") from exc
        return payload

    def patch(self, patch: dict) -> dict:
        with self._lock:
            current = self.read()
            merged = _deep_patch(current, patch)
            return self.write(merged)

    def merged(self, astrbot_config: object) -> dict:
        return merge_settings(self.read(), astrbot_config)


__all__ = [
    "SETTINGS_VERSION",
    "SettingsStore",
    "merge_settings",
    "validate_settings_patch",
]
