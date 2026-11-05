"""语音工坊 · AstrBot 插件（完全独立版 v0.2.0）。

本插件不再依赖 DSH：TTS 由插件自己直接调用各提供商的 HTTP API，参考音色库放在
插件目录的 ``data/voices/``，情绪用内置词典，翻唱只依赖独立的 ``rvc-worker``，
语音条通过 AstrBot 自身的 OneBot v11 适配器发送。

已核实的 AstrBot API（对照 docs.astrbot.app 与 master 源码，文档 URL 见 README）：

* ``from astrbot.api.star import Context, Star``；
* ``from astrbot.api.event import filter, AstrMessageEvent``；
* ``@filter.command("朗读", alias={"语音说"})``，handler 为 async generator；
* ``from astrbot.api.message_components import Record`` + ``Record.fromFileSystem``
  + ``yield event.chain_result([record])``；
* ``_conf_schema.json`` + ``__init__(self, context, config: AstrBotConfig)``，
  ``from astrbot.api import AstrBotConfig, logger``。

命令：
    /朗读 <文本> [-v 音色] [-e 情绪] [-i 强度] [-p 提供商] [-s 语速]
    /语音说 ...（/朗读 的别名）
    /音色
    /音色导入 <本机绝对路径> [名称]
    /音色删除 <名称|id> [--yes]
    /语音状态
    /翻唱 <歌曲绝对路径> [模型]
"""

from __future__ import annotations

import asyncio
import random
import re
import wave
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star

from . import audio, emotion as emotion_mod, tts_providers, worker
from .common import (
    PLUGIN_NAME,
    PLUGIN_VERSION,
    SONG_SUFFIXES,
    VoiceStudioError,
    conf_bool,
    conf_float,
    conf_int,
    conf_list,
    conf_obj,
    conf_str,
    format_bytes,
    format_seconds,
    load_httpx,
    strip_wake_prefix,
    unquote,
)
from .settings_store import SettingsStore, merge_settings, validate_settings_patch
from .voice_store import VoiceStore
from .web_api import StudioWebAPI

_COMMAND_NAMES = ("语音状态", "音色导入", "音色删除", "语音说", "朗读", "音色", "翻唱")
_SINGING_HINTS = ("唱歌", "唱一首", "唱首歌", "唱个歌", "来首歌", "翻唱", "唱一段", "sing")

_TTS_CHUNK_LIMIT = 30
_TTS_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def split_for_tts(text: str, limit: int = _TTS_CHUNK_LIMIT) -> list[str]:
    """把长文本按句切成小块，避免 IndexTTS 长输入卡死。"""

    pieces = [piece.strip() for piece in _TTS_SENTENCE_SPLIT.split(str(text or "")) if piece.strip()]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if len(piece) > limit:
            if current:
                chunks.append(current)
                current = ""
            for start in range(0, len(piece), limit):
                chunks.append(piece[start:start + limit])
            continue
        if len(current) + len(piece) > limit and current:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return chunks or ([str(text).strip()] if str(text).strip() else [])

_READ_OPTION_RE = re.compile(
    r"^\s*-(?P<flag>[vVeEiIpPsS])(?:\s+|:)(?P<value>\"[^\"]*\"|'[^']*'|\S+)\s*"
)


# ---------------------------------------------------------------------------
# 事件 / 参数解析（模块级，方便单测）
# ---------------------------------------------------------------------------


def _message_text(event: AstrMessageEvent) -> str:
    text = getattr(event, "message_str", None)
    if text is None:
        getter = getattr(event, "get_message_str", None)
        if callable(getter):
            try:
                text = getter()
            except Exception:  # noqa: BLE001
                text = ""
    return str(text or "")


def _group_id(event: AstrMessageEvent) -> str:
    getter = getattr(event, "get_group_id", None)
    if callable(getter):
        try:
            gid = getter()
        except Exception:  # noqa: BLE001
            gid = None
        if gid:
            return str(gid)
    message_obj = getattr(event, "message_obj", None)
    gid = getattr(message_obj, "group_id", "") if message_obj is not None else ""
    return str(gid or "")


def _command_arg(message_str: str) -> str:
    """去掉唤醒前缀与命令名，返回命令后面的原始文本。"""

    text = strip_wake_prefix(message_str)
    for name in sorted(_COMMAND_NAMES, key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name + " ") or text.startswith(name + "\u3000"):
            return text[len(name):].strip()
    return text


def parse_read_args(text: str) -> tuple[dict, str]:
    """解析 /朗读 的前置选项，返回 (options, 正文)。

    支持：``-v 音色``、``-e 情绪``、``-i 0.8``、``-p 提供商``、``-s 1.2``。
    """

    options: dict[str, Any] = {}
    remaining = str(text or "").strip()
    for _ in range(12):
        match = _READ_OPTION_RE.match(remaining)
        if not match:
            break
        flag = match.group("flag").lower()
        value = unquote(match.group("value"))
        if flag == "i":
            try:
                intensity = float(value)
            except (TypeError, ValueError) as exc:
                raise VoiceStudioError(f"情绪强度「{value}」不是数字，例如 -i 0.8") from exc
            options["intensity"] = max(0.0, min(1.0, intensity))
        elif flag == "s":
            try:
                speed = float(value)
            except (TypeError, ValueError) as exc:
                raise VoiceStudioError(f"语速「{value}」不是数字，例如 -s 1.2") from exc
            options["speed"] = max(0.5, min(2.0, speed))
        elif flag == "v":
            options["voice"] = value
        elif flag == "e":
            options["emotion"] = value
        elif flag == "p":
            options["provider"] = value
        remaining = remaining[match.end():].strip()
    return options, remaining


def parse_import_args(text: str) -> tuple[str, str]:
    """解析 ``<本机路径> [名称]``，路径可用引号包住空格。"""

    raw = str(text or "").strip()
    if not raw:
        return "", ""
    if raw[0] in {'"', "'"}:
        quote_char = raw[0]
        end = raw.find(quote_char, 1)
        if end == -1:
            return raw[1:].strip(), ""
        path = raw[1:end].strip()
        rest = raw[end + 1:].strip()
    else:
        parts = raw.split(None, 1)
        path = parts[0]
        rest = parts[1].strip() if len(parts) > 1 else ""
    name = unquote(rest) if rest else ""
    return path, name


def parse_delete_args(text: str) -> tuple[str, bool]:
    """解析 ``<名称|id> [--yes]``；名称允许带空格。"""

    tokens = str(text or "").split()
    confirmed = any(token.lower() in {"--yes", "-y"} for token in tokens)
    rest = [token for token in tokens if token.lower() not in {"--yes", "-y"}]
    target = unquote(" ".join(rest).strip())
    return target, confirmed


def split_cover_args(text: str) -> tuple[str, str]:
    """解析 ``<歌曲路径> [模型]``，路径可用引号包住空格。"""

    raw = str(text or "").strip()
    if not raw:
        return "", ""
    if raw[0] in {'"', "'"}:
        quote_char = raw[0]
        end = raw.find(quote_char, 1)
        if end == -1:
            parts = raw[1:].strip().split(None, 1)
            return parts[0] if parts else "", parts[1].strip() if len(parts) > 1 else ""
        song = raw[1:end].strip()
        rest = raw[end + 1:].strip()
        parts = rest.split(None, 1)
        return song, parts[0] if parts else ""
    parts = raw.split(None, 1)
    song = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    return song, rest.split(None, 1)[0] if rest else ""


def _record_for(path: Path) -> Record:
    try:
        return Record.fromFileSystem(str(path))
    except AttributeError:  # 兼容旧版本
        return Record(file=str(path), url=str(path))


def _pick_output(outputs: list, keywords: tuple[str, ...]) -> dict | None:
    for keyword in keywords:
        for output in outputs:
            if not isinstance(output, dict) or not output.get("fileId"):
                continue
            if keyword in str(output.get("name") or "").lower():
                return output
    return None


# ---------------------------------------------------------------------------
# 插件
# ---------------------------------------------------------------------------



def _resolve_data_dir(plugin_dir: Path) -> Path:
    """Resolve AstrBot's plugin data directory so reinstalls never wipe user data.

    Tries the documented helper first, then derives <AstrBot root>/data/plugin_data
    from the installed package, and only then falls back to the plugin directory.
    """
    candidates = []
    try:
        # Typical layout: <AstrBot root>/data/plugins/<plugin dir>; user data must
        # live outside the plugin directory so reinstalls keep it.
        for ancestor in plugin_dir.parents:
            if ancestor.name == "data":
                candidates.append(ancestor / "plugin_data" / PLUGIN_NAME)
                candidates.append(ancestor / "plugins_data" / PLUGIN_NAME)
                break
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

        candidates.append(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
    except Exception:
        pass
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        candidates.append(Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME)
    except Exception:
        pass
    try:
        import astrbot

        root = Path(astrbot.__file__).resolve().parent.parent
        for name in ("data/plugin_data", "data/plugins_data", "data"):
            candidate = root / name / PLUGIN_NAME
            if (root / name).is_dir():
                candidates.append(candidate)
                break
    except Exception:
        pass
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except Exception:
            continue
    return plugin_dir / "data"


def _migrate_legacy_data(plugin_dir: Path, data_dir: Path) -> None:
    """Copy voices/settings from the old in-plugin-directory data folder once."""
    legacy = plugin_dir / "data"
    if legacy == data_dir or not legacy.is_dir():
        return
    if (data_dir / "settings.json").exists() or (data_dir / "voices.json").exists():
        return
    try:
        import shutil

        data_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(legacy, data_dir, dirs_exist_ok=True)
    except Exception:
        pass


class VoiceStudioPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._plugin_dir = Path(__file__).resolve().parent
        self._data_dir = _resolve_data_dir(self._plugin_dir)
        _migrate_legacy_data(self._plugin_dir, self._data_dir)
        self._http_client = None
        self._voice_store: VoiceStore | None = None
        self._settings_store = SettingsStore(self._data_dir / "settings.json")
        self._web_api: StudioWebAPI | None = None
        try:
            self._web_api = StudioWebAPI(self)
            self._web_api.register(context)
        except Exception:  # noqa: BLE001 - 设置页注册失败不影响命令
            logger.exception("[%s] 注册插件 Web API 失败，设置页不可用", PLUGIN_NAME)

    # -- 生命周期 -----------------------------------------------------------
    async def terminate(self) -> None:  # pragma: no cover - 卸载时调用
        client, self._http_client = self._http_client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    # -- 路径 ---------------------------------------------------------------
    @property
    def plugin_dir(self) -> Path:
        return self._plugin_dir

    def _tmp_dir(self) -> Path:
        path = self._data_dir / "tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _voice_dir(self) -> Path:
        raw = conf_str(self.config, "voiceDir", "voices") or "voices"
        path = Path(raw).expanduser()
        if not path.is_absolute():
            parts = list(path.parts)
            if parts and parts[0] == "data":
                parts = parts[1:]
            path = self._data_dir.joinpath(*parts) if parts else self._data_dir / "voices"
        return path

    def _store(self) -> VoiceStore:
        if self._voice_store is None:
            self._voice_store = VoiceStore(self._voice_dir())
        return self._voice_store

    def _cleanup_tmp(self, max_age_sec: float = 24 * 3600) -> None:
        root = self._data_dir / "tmp"
        if not root.is_dir():
            return
        now = time.time()
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if now - entry.stat().st_mtime <= max_age_sec:
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    # -- HTTP ---------------------------------------------------------------
    async def _http(self):
        if self._http_client is not None:
            return self._http_client
        httpx = load_httpx()
        read_timeout = max(30.0, conf_float(self.config, "requestTimeoutSec", 600.0))
        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=read_timeout,
                write=600.0,
                pool=10.0,
            ),
            follow_redirects=True,
        )
        return self._http_client

    # -- 设置（data/settings.json > AstrBot 配置） --------------------------
    def merged_settings(self) -> dict:
        """合并设置页写入的 data/settings.json 与 AstrBot 插件配置。"""

        try:
            return self._settings_store.merged(self.config)
        except VoiceStudioError:
            logger.warning(
                "[%s] 读取 data/settings.json 失败，回退到 AstrBot 插件配置", PLUGIN_NAME
            )
            return merge_settings({}, self.config)

    def save_settings_payload(self, payload: dict) -> dict:
        """保存设置页提交的补丁（校验在 settings_store 里做）。"""

        patch = validate_settings_patch(payload)
        self._settings_store.patch(patch)
        return self.merged_settings()

    def effective_worker(self) -> dict:
        merged = self.merged_settings()
        worker_cfg = merged.get("worker")
        return dict(worker_cfg) if isinstance(worker_cfg, dict) else {}

    # -- 配置解析 -----------------------------------------------------------
    def _provider(self) -> str:
        provider = str(self.merged_settings().get("ttsProvider") or "indextts")
        if provider not in tts_providers.PROVIDERS:
            raise VoiceStudioError(
                f"ttsProvider 配置「{provider}」不可用，可选："
                + " / ".join(tts_providers.PROVIDER_IDS)
            )
        return provider

    def _provider_cfg(self, provider: str) -> dict:
        providers = self.merged_settings().get("providers")
        if not isinstance(providers, dict):
            return {}
        cfg = providers.get(provider)
        return dict(cfg) if isinstance(cfg, dict) else {}

    def _provider_status(self, provider: str) -> tuple[bool, str]:
        meta = tts_providers.PROVIDERS.get(provider)
        if meta is None:
            return False, f"未知提供商 {provider}"
        cfg = self._provider_cfg(provider)
        base_url = conf_str(cfg, "baseUrl", "")
        if not base_url:
            return False, "baseUrl 未配置"
        if meta["kind"] == "online" and not conf_str(cfg, "apiKey", ""):
            return False, "apiKey 未配置"
        if meta["needs_ref"]:
            store = self._store()
            requested = str(self.merged_settings().get("defaultVoice") or "")
            if requested:
                try:
                    store.require(requested)
                except VoiceStudioError as exc:
                    return False, str(exc)
                return True, ""
            available = [voice for voice in store.list_voices() if voice.get("available")]
            if not available:
                return False, "本地提供商需要参考音色，但音色库是空的（可在设置页上传）"
        return True, ""

    def _resolve_ref_voice(self, provider: str, override: str | None) -> dict:
        default_voice = str(self.merged_settings().get("defaultVoice") or "")
        requested = str(override or default_voice).strip()
        store = self._store()
        if requested:
            return store.require(requested)
        voices = [voice for voice in store.list_voices() if voice.get("available")]
        if not voices:
            raise VoiceStudioError(
                "本地提供商需要参考音色：音色库是空的。\n"
                "先在「语音工坊」设置页上传参考音频，或用 /音色导入 <本机音频绝对路径>，"
                "或在设置页把提供商换成在线提供商。"
            )
        return voices[0]

    def _resolve_emotion(self, text: str, options: dict) -> dict:
        merged = self.merged_settings()
        mode = str(merged.get("defaultEmotion") or "auto")
        base_intensity = conf_float(merged, "emotionIntensity", 0.6)
        result = emotion_mod.resolve_emotion(
            text,
            explicit=options.get("emotion"),
            mode=mode,
            default_intensity=base_intensity,
        )
        override_intensity = options.get("intensity")
        if override_intensity is not None and result.get("label") != "neutral":
            result = dict(result)
            result["intensity"] = max(0.0, min(1.0, float(override_intensity)))
            result["source"] = "explicit"
        return result

    def _resolve_speed(self, options: dict) -> float:
        speed = options.get("speed")
        if speed is None:
            speed = conf_float(self.merged_settings(), "ttsSpeed", 1.0)
        return max(0.5, min(2.0, float(speed or 1.0)))

    async def synthesize_to_file(
        self,
        text: str,
        *,
        provider: str | None = None,
        voice: str | None = None,
        emotion: str | None = None,
        intensity: float | None = None,
        speed: float | None = None,
    ) -> dict:
        """合成一段语音到插件 tmp 目录（/朗读 与设置页试听共用）。

        返回 ``{path, format, provider, emotion, emotionApplied}``。
        """

        provider_id = str(provider or self._provider()).strip()
        if provider_id not in tts_providers.PROVIDERS:
            raise VoiceStudioError(
                f"未知提供商「{provider_id}」，可选："
                + " / ".join(tts_providers.PROVIDER_IDS)
            )
        cfg = dict(self._provider_cfg(provider_id))
        ref_path: Path | None = None
        if tts_providers.needs_ref(provider_id):
            voice_entry = await asyncio.to_thread(
                self._resolve_ref_voice, provider_id, voice
            )
            ref_path = Path(voice_entry["path"])
        elif voice:
            cfg["voice"] = str(voice)

        emo = self._resolve_emotion(text, {"emotion": emotion, "intensity": intensity})
        resolved_speed = self._resolve_speed({"speed": speed})
        fmt = tts_providers.resolve_format(provider_id, cfg)
        dest = self._tmp_dir() / f"tts_{int(time.time())}_{uuid.uuid4().hex[:8]}.{fmt}"
        client = await self._http()
        result = await tts_providers.synthesize(
            client,
            provider_id,
            cfg,
            text=text,
            ref_path=ref_path,
            emotion=emo,
            speed=resolved_speed,
            dest=dest,
            timeout=conf_float(self.config, "requestTimeoutSec", 600.0),
        )
        return {
            "path": Path(result["path"]),
            "format": str(result.get("format") or fmt),
            "provider": provider_id,
            "emotion": emo,
            "emotionApplied": bool(result.get("emotionApplied")),
        }

    async def worker_health(self) -> dict:
        merged = self.effective_worker()
        base = worker.normalize_base(str(merged.get("url") or ""))
        client = await self._http()
        return await worker.health(client, base, str(merged.get("token") or ""))

    async def worker_process(
        self, process: str, action: str, port: int | None = None
    ) -> dict:
        merged = self.effective_worker()
        base = worker.normalize_base(str(merged.get("url") or ""))
        url = f"{base}/v1/process/{process}/{action}"
        payload = {"port": int(port)} if port else None
        client = await self._http()
        return await worker.api_json(
            client, "POST", url, token=str(merged.get("token") or ""), json=payload
        )

    def _check_allowed(self, event: AstrMessageEvent) -> str | None:
        gid = _group_id(event)
        if not gid:
            return None
        allowed = [
            str(item).strip()
            for item in conf_list(self.config, "allowedGroups", [])
            if str(item).strip()
        ]
        if not allowed:
            return None
        if gid in allowed:
            return None
        return f"⛔ 本群（{gid}）不在语音工坊白名单里，管理员可在插件配置 allowedGroups 中添加。"

    # -- 钩子：随机语音回复 ------------------------------------------------
    @filter.on_decorating_result()
    async def random_voice_hook(self, event: AstrMessageEvent):
        """开启随机语音后，优先用语音工坊接管回复（失败回退官方 TTS）。"""

        try:
            settings = self.merged_settings()
            chain = None
            result = event.get_result()
            if result is not None:
                chain = getattr(result, "chain", None)
            if settings.get("randomVoiceEnabled"):
                logger.info(
                    "[%s] 随机语音钩子：chain=%s captured=%s takeover=%s",
                    PLUGIN_NAME,
                    [type(item).__name__ for item in (chain or [])],
                    len(str(getattr(event, "_voice_studio_llm_text", "") or "")),
                    bool(settings.get("randomVoiceTakeover", True)),
                )
            await self._maybe_random_voice(event, settings)
        except Exception:  # noqa: BLE001 - 不让钩子影响正常回复
            logger.exception("[%s] 随机语音回复失败", PLUGIN_NAME)

    @filter.on_llm_request()
    async def inject_voice_hint(self, event: AstrMessageEvent, request) -> None:
        """开启随机语音后告诉模型：回复会被自动转成语音条，不要拒绝或索要权限。"""

        try:
            settings = self.merged_settings()
            if not bool(settings.get("randomVoiceEnabled")):
                return
            prompt = getattr(request, "system_prompt", "") or ""
            if "[语音工坊]" in prompt:
                return
            hint = (
                "\n\n[语音工坊] 你的文字回复会被系统自动合成为语音条发给对方，"
                "你不需要、也没有能力自己生成音频文件、发送 record 或申请权限。"
                "不要回答“无法发送语音”“没有音频文件”“需要开启 Python/Shell 权限”之类的话；"
                "正常用文字回答即可，系统会把这段文字转成语音。"
                "当用户要求唱歌/翻唱时，你要说明需要歌曲文件，并使用 /唱歌 <歌曲绝对路径>（RVC 翻唱）；"
                "不要用文字歌词代替唱歌，也不要声称自己现在就能唱。"
            )
            setattr(request, "system_prompt", str(prompt) + hint)
        except Exception:  # noqa: BLE001 - 提示注入失败不影响正常对话
            pass

    @filter.on_llm_response()
    async def capture_llm_text(self, event: AstrMessageEvent, response) -> None:
        """记录本轮 LLM 回复文本，供结果装饰阶段优先用语音工坊接管。"""

        try:
            text = getattr(response, "completion_text", None)
            if text is None:
                text = getattr(response, "text", None)
            if text:
                setattr(event, "_voice_studio_llm_text", str(text))
        except Exception:  # noqa: BLE001
            pass

    async def _maybe_random_voice(self, event: AstrMessageEvent, settings: dict | None = None) -> None:
        settings = settings if settings is not None else self.merged_settings()
        if not bool(settings.get("randomVoiceEnabled")):
            return
        if self._check_allowed(event):
            return
        message_text = _message_text(event).strip()
        if not message_text:
            return
        # 命令（/朗读、/翻唱 等）本身就会发语音，不走随机替换
        if message_text.startswith(("/", "／")):
            return
        first_token = message_text.split()[0]
        if first_token in _COMMAND_NAMES:
            return
        result = event.get_result()
        chain = getattr(result, "chain", None) if result is not None else None
        takeover = bool(settings.get("randomVoiceTakeover", True))
        captured = str(getattr(event, "_voice_studio_llm_text", "") or "").strip()
        pieces: list[str] = []
        text = ""
        if chain:
            for component in chain:
                piece = getattr(component, "text", None)
                if piece is None:
                    pieces = []
                    break
                pieces.append(str(piece))
            text = "".join(pieces).strip()
        # 官方 TTS 可能先把文字换成了语音；接管模式下用捕获的 LLM 原文再合成一次
        if not text and takeover and captured:
            text = captured
        if not text:
            logger.info("[%s] 随机语音跳过：没有可用文本（chain=%s captured=%s）", PLUGIN_NAME, bool(chain), len(captured))
            return
        haystack = (message_text + " " + text).lower()
        if any(hint in haystack for hint in _SINGING_HINTS):
            logger.info("[%s] 随机语音跳过：检测到唱歌/翻唱意图，交由 RVC 翻唱处理", PLUGIN_NAME)
            return
        limit = int(settings.get("randomVoiceMaxChars") or 300)
        if len(text) < 2 or len(text) > limit:
            logger.info("[%s] 随机语音跳过：文本长度 %s 不在 2~%s 内", PLUGIN_NAME, len(text), limit)
            return
        if not takeover:
            probability = float(settings.get("randomVoiceProbability") or 0.0)
            if probability <= 0 or random.random() >= probability:
                return
        provider = str(settings.get("ttsProvider") or "indextts")
        meta = tts_providers.PROVIDERS.get(provider) or {}
        supports_emotion = str(meta.get("supports_emotion") or "none") != "none"
        emotion = str(settings.get("defaultEmotion") or "auto")
        if not supports_emotion or emotion in ("", "auto"):
            emotion = None
        chunks = split_for_tts(text)
        wav_parts: list[Path] = []
        try:
            for index, chunk in enumerate(chunks):
                audio_result = await asyncio.wait_for(
                    self.synthesize_to_file(
                        chunk,
                        provider=None,
                        voice=str(settings.get("defaultVoice") or "") or None,
                        emotion=emotion,
                        intensity=float(settings.get("emotionIntensity") or 0.6),
                    ),
                    timeout=60.0,
                )
                part = await asyncio.wait_for(
                    audio.ensure_wav(Path(audio_result["path"]), self._tmp_dir()),
                    timeout=30.0,
                )
                wav_parts.append(Path(part))
                logger.info("[%s] 语音工坊分段 %s/%s 完成（%s 字）", PLUGIN_NAME, index + 1, len(chunks), len(chunk))
            audio_path = self._concat_wavs(wav_parts)
        except asyncio.TimeoutError:
            logger.warning("[%s] 语音工坊合成超时，回退官方 TTS", PLUGIN_NAME)
            return
        except Exception:  # noqa: BLE001 - 失败时保留原结果，交给官方 TTS 兜底
            logger.exception("[%s] 语音工坊接管失败，回退官方 TTS", PLUGIN_NAME)
            return
        logger.info("[%s] 语音工坊接管回复为语音：%s 字，%s 段", PLUGIN_NAME, len(text), len(chunks))
        result.chain = [_record_for(audio_path)]

    # -- 命令：/朗读 --------------------------------------------------------
    @filter.command("朗读", alias={"语音说"})
    async def tts_cmd(self, event: AstrMessageEvent):
        """用内置的 TTS 提供商合成语音并发送语音条。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        raw = _command_arg(_message_text(event))
        if not raw:
            yield event.plain_result(self._read_usage())
            return
        try:
            options, text = parse_read_args(raw)
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ {exc}")
            return
        if not text:
            yield event.plain_result(self._read_usage())
            return

        max_len = max(1, conf_int(self.config, "maxTextLen", 1000))
        notice = ""
        if len(text) > max_len:
            text = text[:max_len]
            notice = f"（文本超过 {max_len} 字，已截断）"

        try:
            result = await self.synthesize_to_file(
                text,
                provider=options.get("provider"),
                voice=options.get("voice"),
                emotion=options.get("emotion"),
                intensity=options.get("intensity"),
                speed=options.get("speed"),
            )
            audio_path = await audio.ensure_wav(Path(result["path"]), self._tmp_dir())
            if notice:
                yield event.plain_result(notice)
            yield event.chain_result([_record_for(audio_path)])
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 朗读失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，不让插件崩
            logger.exception("[%s] /朗读 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 朗读失败：{exc.__class__.__name__}: {exc}")

    def _concat_wavs(self, paths: list[Path]) -> Path:
        """把多段 wav 顺序拼接成一个 wav（用于长文本分段合成）。"""

        if not paths:
            raise VoiceStudioError("没有可拼接的音频")
        if len(paths) == 1:
            return Path(paths[0])
        dest = self._tmp_dir() / f"voice_studio_join_{uuid.uuid4().hex[:8]}.wav"
        with wave.open(str(paths[0]), "rb") as first:
            params = first.getparams()
            frames = first.readframes(first.getnframes())
        with wave.open(str(dest), "wb") as out:
            out.setparams(params)
            out.writeframes(frames)
            for path in paths[1:]:
                with wave.open(str(path), "rb") as part:
                    out.writeframes(part.readframes(part.getnframes()))
        return dest

    @staticmethod
    def _read_usage() -> str:
        return (
            "用法：/朗读 <文本> [选项]\n"
            "选项：-v <音色|名称>  -e <情绪>  -i <0~1 强度>  -p <提供商>  -s <语速>\n"
            "示例：/朗读 今天真开心 -e happy -i 0.8\n"
            "示例：/朗读 你好 -v 小鲸鱼 -p indextts\n"
            "情绪可选：happy/angry/sad/fear/disgust/down/surprise/calm/neutral 或中文（开心/生气/…）"
        )

    # -- 命令：/音色 --------------------------------------------------------
    @filter.command("音色")
    async def voices_cmd(self, event: AstrMessageEvent):
        """列出本机参考音色库。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        try:
            store = self._store()
            voices = await asyncio.to_thread(store.list_voices)
            stats = await asyncio.to_thread(store.stats)
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 读取音色库失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] /音色 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 读取音色库失败：{exc.__class__.__name__}: {exc}")
            return

        merged = self.merged_settings()
        provider = str(merged.get("ttsProvider") or "indextts")
        default_voice = str(merged.get("defaultVoice") or "")

        if not voices:
            yield event.plain_result(
                "🎧 音色库是空的。\n"
                f"目录：{stats['dir']}\n"
                "用 /音色导入 <本机音频绝对路径> [名称] 导入一段参考音频（wav/mp3/flac 等，≤30MB）。"
            )
            return

        lines = [f"🎧 参考音色 {len(voices)} 个（{stats['available']} 个可用）"]
        if default_voice:
            lines.append(f"当前 defaultVoice：{default_voice}")
        limit = 40
        for index, voice in enumerate(voices[:limit], start=1):
            missing = "" if voice.get("available") else " · [文件缺失]"
            lines.append(
                f"{index}. {voice.get('name')}  ·  id: {voice.get('id')}"
                f"  ·  {format_bytes(voice.get('size'))}{missing}"
            )
        if len(voices) > limit:
            lines.append(f"…还有 {len(voices) - limit} 个未列出")
        lines.append(
            f"当前提供商：{tts_providers.label_of(provider)}（{provider}）。"
            + (
                "本地提供商发语音时使用参考音色。"
                if tts_providers.needs_ref(provider)
                else "在线提供商使用自己的 voice 配置。"
            )
        )
        lines.append("导入：/音色导入 <本机绝对路径> [名称]；删除：/音色删除 <名称|id> --yes")
        yield event.plain_result("\n".join(lines))

    # -- 命令：/音色导入 ----------------------------------------------------
    @filter.command("音色导入")
    async def voice_import_cmd(self, event: AstrMessageEvent):
        """把本机音频复制进音色库。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        raw = _command_arg(_message_text(event))
        if not raw:
            yield event.plain_result(
                "用法：/音色导入 <本机音频绝对路径> [名称]\n"
                '例如：/音色导入 "C:\\voices\\xiaojingyu.wav" 小鲸鱼'
            )
            return
        path, name = parse_import_args(raw)
        if not path:
            yield event.plain_result("用法：/音色导入 <本机音频绝对路径> [名称]")
            return
        try:
            store = self._store()
            entry = await asyncio.to_thread(store.add, path, name or None)
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 导入失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] /音色导入 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 导入失败：{exc.__class__.__name__}: {exc}")
            return
        yield event.plain_result(
            f"✅ 已导入音色「{entry['name']}」（id: {entry['id']}，{format_bytes(entry['size'])}）。\n"
            f"可以这样用：/朗读 -v {entry['id']} 你好\n"
            f"或把 defaultVoice 配置成 {entry['id']}。"
        )

    # -- 命令：/音色删除 ----------------------------------------------------
    @filter.command("音色删除")
    async def voice_delete_cmd(self, event: AstrMessageEvent):
        """从音色库删除一个音色（需要 --yes 二次确认）。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        raw = _command_arg(_message_text(event))
        target, confirmed = parse_delete_args(raw)
        if not target:
            yield event.plain_result(
                "用法：/音色删除 <名称|id> [--yes]\n不加 --yes 会先让你确认。"
            )
            return

        store = self._store()
        try:
            voice = await asyncio.to_thread(store.resolve, target)
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 删除失败：{exc}")
            return
        if voice is None:
            yield event.plain_result(f"❌ 音色库里找不到「{target}」。用 /音色 查看已有音色。")
            return
        if not confirmed:
            yield event.plain_result(
                f"⚠️ 确认删除音色「{voice.get('name')}」（id: {voice.get('id')}）？此操作不可恢复。\n"
                f"回复：/音色删除 {voice.get('id')} --yes"
            )
            return
        try:
            removed = await asyncio.to_thread(store.delete, target)
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 删除失败：{exc}")
            return
        yield event.plain_result(
            f"🗑️ 已删除音色「{removed.get('name')}」（id: {removed.get('id')}）。"
        )

    # -- 命令：/语音状态 ----------------------------------------------------
    @filter.command("语音状态")
    async def status_cmd(self, event: AstrMessageEvent):
        """检查本插件自己的配置、音色库与 rvc-worker 连通性。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        merged = self.merged_settings()
        provider = str(merged.get("ttsProvider") or "indextts")
        meta = tts_providers.PROVIDERS.get(provider)
        lines = [f"🩺 语音工坊（AstrBot 独立版 v{PLUGIN_VERSION}）状态"]

        if meta is None:
            lines.append(f"❌ TTS 提供商：未知「{provider}」")
        else:
            ok, reason = self._provider_status(provider)
            icon = "✅" if ok else "⚠️"
            detail = "" if ok else f" — {reason}"
            lines.append(
                f"{icon} TTS 提供商：{tts_providers.label_of(provider)}（{provider}）{detail}"
            )

        try:
            stats = await asyncio.to_thread(self._store().stats)
            lines.append(
                f"✅ 音色库：{stats['dir']}（{stats['count']} 个，{stats['available']} 个可用）"
            )
            default_voice = str(merged.get("defaultVoice") or "")
            if default_voice:
                voice = await asyncio.to_thread(self._store().resolve, default_voice)
                if voice is None:
                    lines.append(f"⚠️ defaultVoice「{default_voice}」不在音色库里")
        except VoiceStudioError as exc:
            lines.append(f"❌ 音色库：不可用（{exc}）")

        try:
            worker_cfg = merged.get("worker") if isinstance(merged.get("worker"), dict) else {}
            base = worker.normalize_base(str(worker_cfg.get("url") or ""))
            token = str(worker_cfg.get("token") or "")
            client = await self._http()
            health = await worker.health(client, base, token)
            rvc = health.get("rvc") if isinstance(health.get("rvc"), dict) else {}
            gpu = health.get("gpu") if isinstance(health.get("gpu"), dict) else {}
            jobs = health.get("jobs") if isinstance(health.get("jobs"), dict) else {}
            model_list = rvc.get("models")
            model_count = len(model_list) if isinstance(model_list, list) else "?"
            lines.append(
                "✅ rvc-worker：可用"
                f"（v{health.get('version') or '?'}，RVC {'可用' if rvc.get('available') else '不可用'}，"
                f"模型 {model_count} 个，GPU {gpu.get('name') or '未知'}，"
                f"进行中任务 {jobs.get('running', 0)}）"
            )
        except VoiceStudioError as exc:
            lines.append(f"❌ rvc-worker：不可用（{exc}）")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] /语音状态 读取 worker 失败", PLUGIN_NAME)
            lines.append(f"❌ rvc-worker：不可用（{exc.__class__.__name__}: {exc}）")

        lines.append("本插件不依赖 DSH；TTS 由插件直接请求各提供商 API。")
        yield event.plain_result("\n".join(lines))

    # -- 命令：/翻唱 --------------------------------------------------------
    @filter.command("翻唱", alias={"唱歌"})
    async def cover_cmd(self, event: AstrMessageEvent):
        """上传歌曲到 rvc-worker，分离 + 换声 + 本地混音后发送语音条。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        raw = _command_arg(_message_text(event))
        if not raw:
            yield event.plain_result(
                "用法：/翻唱 <歌曲绝对路径> [模型]\n"
                '例如：/翻唱 "C:\\music\\song.mp3"\n'
                "提示：路径有空格时用英文引号包住；模型留空则用配置 workerModel，"
                "再留空则用 worker 上的第一个模型。"
            )
            return

        song_raw, model = split_cover_args(raw)
        song_path = Path(song_raw).expanduser()
        if not song_path.is_absolute():
            yield event.plain_result(f"❌ 请给歌曲的绝对路径（收到：{song_raw}）")
            return
        try:
            is_file = song_path.is_file()
        except OSError:
            is_file = False
        if not is_file:
            yield event.plain_result(f"❌ 找不到歌曲文件：{song_path}")
            return
        if song_path.suffix.lower() not in SONG_SUFFIXES:
            yield event.plain_result(
                f"❌ 不支持的音频格式：{song_path.suffix}，支持 "
                + " / ".join(sorted(SONG_SUFFIXES))
            )
            return

        worker_settings = self.effective_worker()
        progress = conf_bool(self.config, "sendProgress", True)
        max_duration = max(10, conf_int(self.config, "maxDurationSec", 600))
        job_timeout = max(60, conf_int(self.config, "jobTimeoutSec", 1800))
        f0_method = str(worker_settings.get("f0Method") or "rmvpe")
        try:
            key_shift = int(worker_settings.get("keyShift") or 0)
        except (TypeError, ValueError):
            key_shift = 0
        if not model:
            model = str(worker_settings.get("model") or "")

        try:
            base = worker.normalize_base(str(worker_settings.get("url") or ""))
            token = str(worker_settings.get("token") or "")
            client = await self._http()
            await asyncio.to_thread(self._cleanup_tmp)

            duration = await audio.probe_duration(song_path)
            if duration is not None and duration > max_duration:
                raise VoiceStudioError(
                    f"歌曲时长 {format_seconds(duration)} 超过上限 {max_duration} 秒"
                    "（可在配置 maxDurationSec 调整）。"
                )
            if progress:
                yield event.plain_result(
                    f"🎵 开始翻唱：{song_path.name}（{format_seconds(duration)}）\n"
                    "流程：上传 → 人声分离 → 换声 → 混音，可能需要几分钟…"
                )

            if not model:
                model = await self._first_model(client, base, token)

            file_id = await worker.upload_file(client, base, token, song_path)
            if progress:
                yield event.plain_result("📤 上传完成，正在做人声分离…")
            separate_job = await worker.create_job(
                client, base, token, "separate", file_id, {"mode": "vocals", "format": "wav"}
            )
            separate = await worker.wait_job(
                client, base, token, separate_job, timeout_s=job_timeout, logger=logger
            )
            outputs = separate.get("outputs") or []
            if not isinstance(outputs, list):
                outputs = []
            vocal_out = _pick_output(outputs, ("vocal", "人声"))
            inst_out = _pick_output(outputs, ("instrument", "accompaniment", "inst", "伴奏"))
            if vocal_out is None or inst_out is None:
                raise VoiceStudioError(
                    "分离任务的输出里没有 vocal / instrumental："
                    + str([item.get("name") for item in outputs if isinstance(item, dict)])
                )

            work_dir = self._tmp_dir() / f"cover_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            work_dir.mkdir(parents=True, exist_ok=True)
            vocal_wav = work_dir / "vocal.wav"
            inst_wav = work_dir / "instrumental.wav"
            await worker.download_job_output(
                client, base, token, separate_job, str(vocal_out.get("name") or "vocal.wav"), vocal_wav
            )
            await worker.download_job_output(
                client, base, token, separate_job, str(inst_out.get("name") or "instrumental.wav"), inst_wav
            )

            actual_duration = audio.wav_duration_sec(inst_wav)
            if actual_duration is not None and actual_duration > max_duration:
                raise VoiceStudioError(
                    f"歌曲实际时长 {format_seconds(actual_duration)} 超过上限 {max_duration} 秒。"
                )

            if progress:
                yield event.plain_result(f"🎤 分离完成，正在换声（模型：{model}，变调：{key_shift:+d}）…")

            convert_params: dict[str, Any] = {
                "model": model,
                "f0_method": f0_method,
                "f0_up_key": int(key_shift),
                "index_rate": 0.75,
                "rms_mix": 0.25,
                "protect": 0.33,
            }
            index = str(worker_settings.get("index") or "")
            if index:
                convert_params["index"] = index
            convert_job = await worker.create_job(
                client, base, token, "convert", str(vocal_out.get("fileId")), convert_params
            )
            converted = await worker.wait_job(
                client, base, token, convert_job, timeout_s=job_timeout, logger=logger
            )
            converted_outputs = converted.get("outputs") or []
            if not isinstance(converted_outputs, list):
                converted_outputs = []
            converted_out = _pick_output(converted_outputs, ("convert", "vocal", "人声"))
            if converted_out is None:
                converted_out = next(
                    (item for item in converted_outputs if isinstance(item, dict) and item.get("fileId")),
                    None,
                )
            if converted_out is None:
                raise VoiceStudioError("换声任务的输出里没有音频文件")
            converted_wav = work_dir / "converted.wav"
            await worker.download_job_output(
                client, base, token, convert_job, str(converted_out.get("name") or "converted.wav"), converted_wav
            )

            if progress:
                yield event.plain_result("🎚️ 换声完成，正在混音…")
            mixed = work_dir / "cover.wav"
            await asyncio.to_thread(audio.mix_cover, inst_wav, converted_wav, vocal_wav, mixed)
            final = await self._finalize_cover(mixed)
            yield event.chain_result([_record_for(final)])
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 翻唱失败：{exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] /翻唱 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 翻唱失败：{exc.__class__.__name__}: {exc}")

    async def _first_model(self, client, base: str, token: str) -> str:
        data = await worker.models(client, base, token)
        models = data.get("models")
        if not isinstance(models, list) or not models:
            raise VoiceStudioError(
                "rvc-worker 上没有可用的 RVC 模型（GET /v1/models 返回空），"
                "请把 .pth 放进 RVC 的 assets/weights 后重试，或配置 workerModel。"
            )
        first = models[0]
        model_id = ""
        if isinstance(first, dict):
            model_id = str(first.get("id") or first.get("name") or "").strip()
        else:
            model_id = str(first).strip()
        if not model_id:
            raise VoiceStudioError("无法从 rvc-worker 的模型清单里解析出模型 id")
        return model_id

    async def _finalize_cover(self, mixed: Path) -> Path:
        """可选 ffmpeg loudnorm 统一响度；没有 ffmpeg 就用原始混音文件。"""

        if not audio.which("ffmpeg"):
            return mixed
        out = mixed.with_name(f"{mixed.stem}_norm.wav")
        ok = await audio.run_ffmpeg(
            [
                "-y",
                "-i",
                str(mixed),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11",
                "-ar",
                "44100",
                str(out),
            ]
        )
        if ok and out.is_file() and out.stat().st_size > 0:
            return out
        return mixed


__all__ = [
    "VoiceStudioPlugin",
    "parse_read_args",
    "parse_import_args",
    "parse_delete_args",
    "split_cover_args",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
]
