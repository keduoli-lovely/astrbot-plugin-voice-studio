"""设置页的后端 Web API（AstrBot 插件可视化视图）。

路由都注册在 ``/{plugin_dir_name}/...`` 下，前端通过
``window.AstrBotPluginView`` bridge 的 ``apiGet`` / ``apiPost`` / ``upload`` 调用。

已核实的 AstrBot API（master 源码 ``astrbot/api/web.py`` + 官方文档「插件可视化视图」）：

* ``context.register_web_api(route, handler, methods, desc)``
* ``from astrbot.api.web import request, json_response, error_response, file_response, PluginUploadFile``
* ``await request.json(default={})`` / ``await request.form()`` / ``await request.files()``
* 上传字段名由 bridge 固定为 ``file``；``PluginUploadFile`` 有 ``filename`` /
  ``content_length`` / ``await read(size)`` / ``await save(path, max_bytes=...)``

本模块不在 AstrBot 环境下也能 import（用于离线单测）：缺少 ``astrbot.api.web`` 时
``WEB_API_AVAILABLE = False``，插件仍可只用命令运行。
"""

from __future__ import annotations

import asyncio
import base64
import time
import uuid
import mimetypes
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import audio, tts_providers, worker
from .common import (
    MAX_VOICE_BYTES,
    PLUGIN_NAME,
    VoiceStudioError,
    format_bytes,
)
from .settings_store import validate_settings_patch
from .voice_store import VoiceStore

try:  # pragma: no cover - 真实 AstrBot 环境
    from astrbot.api.web import (
        PluginUploadFile,
        error_response,
        file_response,
        json_response,
        request,
    )

    WEB_API_AVAILABLE = True
except ImportError:  # 旧版 AstrBot / 离线测试：占位，不注册路由
    WEB_API_AVAILABLE = False
    PluginUploadFile = object  # type: ignore[assignment,misc]

    def json_response(data=None, **_kwargs):  # type: ignore[misc]
        return {"ok": True, "data": data}

    def error_response(message: str, **_kwargs):  # type: ignore[misc]
        return {"status": "error", "message": message}

    def file_response(path, **_kwargs):  # type: ignore[misc]
        return {"path": str(path)}

    request = None  # type: ignore[assignment]


PREVIEW_MAX_CHARS = 20
PREVIEW_MAX_BYTES = 40_000
PREVIEW_CHUNK_BYTES = 4_096
PREVIEW_TRANSCODE_ABOVE = 4_000
PROCESS_ALLOWED = {"rvc-webui": "RVC 进程", "msst": "MSST 进程"}
ACTION_ALLOWED = {"start", "stop"}

_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".aac": "audio/aac",
    ".wma": "audio/x-ms-wma",
    ".aiff": "audio/aiff",
    ".aif": "audio/aiff",
}


def provider_meta() -> dict:
    """给设置页用的提供商元数据（标签/类型/是否要参考音色/默认格式）。"""

    return {
        provider_id: {
            "label": meta["label"],
            "kind": meta["kind"],
            "needsRef": bool(meta["needs_ref"]),
            "supportsEmotion": meta["supports_emotion"],
            "defaultFormat": meta["default_format"],
        }
        for provider_id, meta in tts_providers.PROVIDERS.items()
    }


def _mime_for(path: Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _MIME:
        return _MIME[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class StudioWebAPI:
    """把插件能力包装成设置页可调用的 HTTP API。"""

    def __init__(self, plugin):
        self.plugin = plugin
        self.routes: list[str] = []
        self._plugin_name = getattr(getattr(plugin, "plugin_dir", Path(".")), "name", PLUGIN_NAME) or PLUGIN_NAME

    # -- 注册 ---------------------------------------------------------------
    def register(self, context) -> bool:
        if not WEB_API_AVAILABLE:
            logger.warning(
                "[%s] 当前 AstrBot 不支持 astrbot.api.web，设置页不可用（命令仍可用）",
                PLUGIN_NAME,
            )
            return False
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.warning(
                "[%s] 当前 AstrBot 没有 context.register_web_api，设置页不可用（命令仍可用）",
                PLUGIN_NAME,
            )
            return False
        routes = [
            ("settings", self.handle_settings, ["GET", "POST"], "读取/保存语音工坊设置"),
            ("voices", self.handle_voices, ["GET"], "列出参考音色"),
            ("voices/upload", self.handle_voice_upload, ["POST"], "上传参考音色"),
            ("voices/import", self.handle_voice_import, ["POST"], "从服务器路径导入参考音色"),
            ("voices/delete", self.handle_voice_delete, ["POST"], "删除参考音色"),
("voices/rename", self.handle_voice_rename, ["POST"], "重命名参考音色"),
("tts/health", self.handle_tts_health, ["GET"], "语音合成连接测试"),
            ("voices/audio", self.handle_voice_audio, ["GET"], "播放参考音色"),
            ("test", self.handle_test, ["POST"], "试听合成"),
("test/chunk", self.handle_test_chunk, ["GET"], "试听音频分片"),
            ("worker/health", self.handle_worker_health, ["GET"], "rvc-worker 健康检查"),
("worker/models", self.handle_worker_models, ["GET"], "从 rvc-worker 获取模型/索引列表"),
            ("worker/process", self.handle_worker_process, ["POST"], "启动/停止 worker 子进程"),
        ]
        for path, handler, methods, desc in routes:
            try:
                register(f"/{self._plugin_name}/{path}", handler, methods, desc)
                self.routes.append(path)
            except Exception:  # noqa: BLE001 - 某个路由失败不应拖垮插件
                logger.exception(
                    "[%s] 注册设置页路由失败：%s", PLUGIN_NAME, path
                )
        return bool(self.routes)

    # -- 设置 ---------------------------------------------------------------
    async def handle_settings(self):
        try:
            if str(getattr(request, "method", "GET")).upper() == "POST":
                payload = await request.json(default={})
                patch = validate_settings_patch(payload)
                saved = await asyncio.to_thread(
                    self.plugin.save_settings_payload, patch
                )
                return json_response({"ok": True, "settings": saved})
            settings = await asyncio.to_thread(self.plugin.merged_settings)
            return json_response(
                {
                    "ok": True,
                    "settings": settings,
                    "providers": provider_meta(),
                    "dataDir": str(getattr(self.plugin, "_data_dir", "")),
                    "pluginDir": str(getattr(self.plugin, "plugin_dir", "")),
                    "voiceDir": str(getattr(self.plugin, "_voice_dir", "")()),
                }
            )
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 读取/保存设置失败", PLUGIN_NAME)
            return error_response(f"读取/保存设置失败：{exc.__class__.__name__}: {exc}", status_code=500)

    # -- 音色库 -------------------------------------------------------------
    def _store(self) -> VoiceStore:
        return self.plugin._store()

    async def handle_voices(self):
        try:
            voices = await asyncio.to_thread(self._store().list_voices)
            settings = await asyncio.to_thread(self.plugin.merged_settings)
            public = [
                {
                    "id": voice.get("id"),
                    "name": voice.get("name"),
                    "file": voice.get("file"),
                    "size": voice.get("size"),
                    "available": bool(voice.get("available")),
                    "description": voice.get("description") or "",
                    "createdAt": voice.get("createdAt"),
                }
                for voice in voices
            ]
            return json_response(
                {
                    "ok": True,
                    "voices": public,
                    "defaultVoice": settings.get("defaultVoice") or "",
                }
            )
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)

    async def handle_voice_upload(self):
        try:
            files = await request.files()
            upload = files.get("file") if hasattr(files, "get") else None
            if upload is None or not isinstance(upload, PluginUploadFile):
                return error_response("缺少 file 表单字段（bridge.upload 固定字段名 file）", status_code=400)
            filename = str(getattr(upload, "filename", "") or "")
            if not filename:
                return error_response("上传文件没有文件名", status_code=400)
            content_length = getattr(upload, "content_length", None)
            try:
                declared = int(content_length) if content_length else 0
            except (TypeError, ValueError):
                declared = 0
            if declared and declared > MAX_VOICE_BYTES:
                return error_response(
                    f"音频文件超过上限 {format_bytes(MAX_VOICE_BYTES)}", status_code=400
                )
            content = await upload.read(MAX_VOICE_BYTES + 1)
            if not isinstance(content, (bytes, bytearray)):
                return error_response("读取上传文件失败", status_code=400)
            if len(content) > MAX_VOICE_BYTES:
                return error_response(
                    f"音频文件超过上限 {format_bytes(MAX_VOICE_BYTES)}", status_code=400
                )
            entry = await asyncio.to_thread(
                self._store().add_bytes, bytes(content), filename
            )
            return json_response({"ok": True, "voice": entry})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 上传音色失败", PLUGIN_NAME)
            return error_response(f"上传失败：{exc.__class__.__name__}: {exc}", status_code=500)

    async def handle_voice_import(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            path = str(payload.get("path") or "").strip()
            name = str(payload.get("name") or "").strip()
            if not path:
                return error_response("请填写服务器上的音频绝对路径", status_code=400)
            entry = await asyncio.to_thread(self._store().add, path, name or None)
            return json_response({"ok": True, "voice": entry})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 从路径导入音色失败", PLUGIN_NAME)
            return error_response(f"导入失败：{exc.__class__.__name__}: {exc}", status_code=500)

    async def handle_voice_delete(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            target = str(payload.get("id") or payload.get("name") or "").strip()
            if not target:
                return error_response("请指定要删除的音色 id 或名称", status_code=400)
            voice = await asyncio.to_thread(self._store().resolve, target)
            if voice is None:
                return error_response(f"音色库里找不到「{target}」", status_code=404)
            removed = await asyncio.to_thread(self._store().delete, target)
            return json_response({"ok": True, "voice": removed})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)

    async def handle_voice_rename(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            key = str(payload.get("id") or payload.get("name") or "").strip()
            new_name = payload.get("newName")
            if not key:
                return error_response("缺少音色 id 或名称", status_code=400)
            entry = await asyncio.to_thread(self._store().rename, key, new_name)
            return json_response({"ok": True, "voice": entry})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 重命名音色失败", PLUGIN_NAME)
            return error_response(f"重命名音色失败：{exc.__class__.__name__}: {exc}", status_code=500)

    async def handle_tts_health(self):
        """语音合成连接测试：本地提供商探测 HTTP，在线提供商只检查 Key。"""
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return error_response("缺少 httpx 依赖，无法测试连接", status_code=500)
        try:
            settings = await asyncio.to_thread(self.plugin.merged_settings)
            provider = str(request.query.get("provider") or settings.get("ttsProvider") or "indextts")
            if provider not in tts_providers.PROVIDERS:
                return error_response(f"未知的 TTS 提供商：{provider}", status_code=400)
            meta = tts_providers.PROVIDERS[provider]
            prov = (settings.get("providers") or {}).get(provider) or {}
            started = time.monotonic()
            if meta.get("kind") == "online":
                key = str(prov.get("apiKey") or "").strip()
                elapsed = int((time.monotonic() - started) * 1000)
                if not key:
                    return json_response({"ok": False, "provider": provider, "detail": "在线提供商未填写 API Key", "elapsedMs": elapsed})
                return json_response({"ok": True, "provider": provider, "detail": "API Key 已配置（在线提供商不发起网络探测）", "elapsedMs": elapsed})
            base_url = str(prov.get("baseUrl") or "").strip().rstrip("/")
            if not base_url:
                return json_response({"ok": False, "provider": provider, "detail": "未填写 baseUrl", "elapsedMs": 0})
            suffix = "/config" if provider == "indextts" else "/docs"
            url = base_url + suffix
            try:
                async with httpx.AsyncClient(timeout=6.0) as client:
                    response = await client.get(url)
                detail = f"GET {url} -> HTTP {response.status_code}"
                ok = response.status_code < 500
                if provider == "indextts" and ok and "gen_single" not in response.text:
                    ok = False
                    detail += "（响应里没有 gen_single，可能不是 IndexTTS）"
                return json_response({"ok": ok, "provider": provider, "detail": detail,
                                      "elapsedMs": int((time.monotonic() - started) * 1000)})
            except Exception as exc:  # noqa: BLE001
                return json_response({"ok": False, "provider": provider,
                                      "detail": f"连接失败：{exc.__class__.__name__}: {exc}",
                                      "elapsedMs": int((time.monotonic() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 测试语音合成连接失败", PLUGIN_NAME)
            return error_response(f"测试连接失败：{exc.__class__.__name__}: {exc}", status_code=500)

    async def handle_voice_audio(self):
        try:
            name = request.query.get("name") if request is not None else None
            key = str(name or "").strip()
            if not key:
                return error_response("缺少 name 参数", status_code=400)
            voice = await asyncio.to_thread(self._store().resolve, key)
            if voice is None:
                return error_response(f"音色库里找不到「{key}」", status_code=404)
            path = Path(str(voice.get("path") or ""))
            if not path.is_file():
                return error_response("音色文件缺失", status_code=404)
            return file_response(
                path,
                filename=f"{voice.get('name') or voice.get('id')}{path.suffix}",
                content_type=_mime_for(path),
            )
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)

    # -- 试听 ---------------------------------------------------------------
    async def handle_test_chunk(self):
        """按 4KB 分片返回试听音频（base64），避免 bridge 大响应被截断。"""
        try:
            preview_id = str(request.query.get("id") or "").strip()
            index_raw = request.query.get("index") or "0"
            if not preview_id or not preview_id.isalnum():
                return error_response("缺少试听 id", status_code=400)
            try:
                index = int(index_raw)
            except (TypeError, ValueError):
                return error_response("试听分片序号无效", status_code=400)
            tmp_dir = self.plugin._data_dir / "tmp"
            matches = list(tmp_dir.glob(f"preview_{preview_id}.*"))
            if not matches:
                return error_response("试听音频已过期，请重新试听", status_code=404)
            content = matches[0].read_bytes()
            offset = index * PREVIEW_CHUNK_BYTES
            if offset >= len(content) or index < 0:
                return error_response("试听分片序号越界", status_code=400)
            chunk = content[offset:offset + PREVIEW_CHUNK_BYTES]
            return json_response(
                {
                    "ok": True,
                    "index": index,
                    "data": base64.b64encode(chunk).decode("ascii"),
                    "done": offset + PREVIEW_CHUNK_BYTES >= len(content),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 试听分片读取失败", PLUGIN_NAME)
            return error_response(f"试听分片失败：{exc}", status_code=500)

    async def handle_test(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            text = str(payload.get("text") or "").strip()
            if not text:
                return error_response("请先填写试听文本", status_code=400)
            if len(text) > PREVIEW_MAX_CHARS:
                return error_response(
                    f"试听文本请控制在 {PREVIEW_MAX_CHARS} 字以内（正式朗读请用 /朗读 命令）",
                    status_code=400,
                )
            provider = str(payload.get("provider") or "").strip() or None
            if provider is not None and provider not in tts_providers.PROVIDERS:
                return error_response(f"未知的 TTS 提供商「{provider}」", status_code=400)
            voice = str(payload.get("voice") or "").strip() or None
            emotion = payload.get("emotion")
            emotion_label = None
            if isinstance(emotion, str) and emotion.strip() and emotion.strip().lower() != "auto":
                emotion_label = emotion.strip()
            intensity = payload.get("intensity")
            try:
                intensity_value = float(intensity) if intensity not in (None, "") else None
            except (TypeError, ValueError):
                intensity_value = None

            result = await self.plugin.synthesize_to_file(
                text,
                provider=provider,
                voice=voice,
                emotion=emotion_label,
                intensity=intensity_value,
            )
            path = Path(result["path"])
            content = path.read_bytes()
            mime = _mime_for(path)
            tmp_dir = self.plugin._data_dir / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            # Always transcode previews to a compact, good-sounding mono MP3.
            # The dashboard iframe receives the audio in small chunks (test/chunk),
            # so the per-response size stays far below the bridge truncation point.
            if audio.which("ffmpeg"):
                mp3_path = tmp_dir / f"preview_{uuid.uuid4().hex[:8]}.mp3"
                ok = await audio.run_ffmpeg(
                    ["-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
                     "-t", "30", "-ac", "1", "-ar", "22050", "-c:a", "libmp3lame", "-b:a", "48k",
                     str(mp3_path)]
                )
                if ok and mp3_path.is_file() and mp3_path.stat().st_size > 0:
                    path = mp3_path
                    content = mp3_path.read_bytes()
                    mime = "audio/mpeg"
            if len(content) > PREVIEW_MAX_BYTES:
                return error_response(
                    "语音合成试听：音频过大（%.1f MB），请缩短文本或换更短的音色"
                    % (len(content) / 1048576.0),
                    status_code=400,
                )
            # Drop stale preview files, then expose the audio as a chunked download.
            cutoff = time.time() - 1800
            for stale in tmp_dir.glob("preview_*.mp3"):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    pass
            preview_id = uuid.uuid4().hex[:16]
            ext = ".mp3" if mime == "audio/mpeg" else path.suffix or ".bin"
            (tmp_dir / f"preview_{preview_id}{ext}").write_bytes(content)
            return json_response(
                {
                    "ok": True,
                    "previewId": preview_id,
                    "mime": mime,
                    "bytes": len(content),
                    "chunks": (len(content) + PREVIEW_CHUNK_BYTES - 1) // PREVIEW_CHUNK_BYTES,
                    "chunkBytes": PREVIEW_CHUNK_BYTES,
                    "format": result.get("format") or path.suffix.lstrip("."),
                    "provider": result.get("provider"),
                    "emotionApplied": bool(result.get("emotionApplied")),
                    "emotion": result.get("emotion"),
                }
            )
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 试听合成失败", PLUGIN_NAME)
            return error_response(f"试听失败：{exc.__class__.__name__}: {exc}", status_code=500)

    # -- worker -------------------------------------------------------------
    async def handle_worker_models(self):
        """从 rvc-worker 的 /v1/models 取模型与索引，前端不写死任何模型名。"""
        try:
            import httpx
        except Exception:  # noqa: BLE001
            return error_response("缺少 httpx 依赖", status_code=500)
        try:
            settings = await asyncio.to_thread(self.plugin.merged_settings)
            worker_cfg = settings.get("worker") or {}
            base_url = worker.normalize_base(str(worker_cfg.get("url") or ""))
            if not base_url:
                return error_response("请先填写 Worker 地址", status_code=400)
            token = str(worker_cfg.get("token") or "").strip()
            async with httpx.AsyncClient(timeout=20.0) as client:
                data = await worker.models(client, base_url, token)
            return json_response(
                {
                    "ok": True,
                    "models": data.get("models") or [],
                    "indexes": data.get("indexes") or [],
                    "root": data.get("root") or data.get("rvcRoot") or "",
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] 获取 worker 模型列表失败", PLUGIN_NAME)
            return error_response(f"获取模型列表失败：{exc}", status_code=502)

    async def handle_worker_health(self):
        try:
            health = await self.plugin.worker_health()
            return json_response({"ok": True, "health": health})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] worker 健康检查失败", PLUGIN_NAME)
            return error_response(f"worker 健康检查失败：{exc.__class__.__name__}: {exc}", status_code=500)

    async def handle_worker_process(self):
        try:
            payload = await request.json(default={})
            if not isinstance(payload, dict):
                return error_response("请求体必须是 JSON 对象", status_code=400)
            process = str(payload.get("process") or "").strip()
            action = str(payload.get("action") or "").strip()
            if process not in PROCESS_ALLOWED:
                return error_response(
                    "process 只能是 " + " / ".join(PROCESS_ALLOWED), status_code=400
                )
            if action not in ACTION_ALLOWED:
                return error_response(
                    "action 只能是 " + " / ".join(sorted(ACTION_ALLOWED)), status_code=400
                )
            port = payload.get("port")
            try:
                port_value = int(port) if port not in (None, "") else None
            except (TypeError, ValueError):
                return error_response("port 必须是整数", status_code=400)
            result = await self.plugin.worker_process(process, action, port_value)
            return json_response({"ok": True, "result": result})
        except VoiceStudioError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] worker 进程操作失败", PLUGIN_NAME)
            return error_response(f"worker 进程操作失败：{exc.__class__.__name__}: {exc}", status_code=500)


__all__ = [
    "PREVIEW_MAX_BYTES",
    "PREVIEW_MAX_CHARS",
    "PROCESS_ALLOWED",
    "StudioWebAPI",
    "WEB_API_AVAILABLE",
    "provider_meta",
]
