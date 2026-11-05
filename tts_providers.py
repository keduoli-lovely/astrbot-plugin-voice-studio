"""TTS 提供商适配器 —— 移植自 DSH 语音工坊 ``lib/tts.js``（纯 httpx 实现）。

本模块完全独立：只调用各提供商自己的 HTTP API，不经过 DSH。

============  ==========================================  ==========  ==============
id            协议                                         类型        情绪
============  ==========================================  ==========  ==============
indextts      Gradio 5：/gradio_api/upload + call/gen_single  本地        8 维向量
gptsovits     api_v2.py：GET|POST /tts                       本地        无
siliconflow   POST /v1/audio/speech                        在线        CosyVoice 前缀
minimax       POST /v1/t2a_v2（audio 为 hex）               在线        枚举
mimo          POST /v1/chat/completions + audio 参数        在线        中文风格前缀
openai        POST /v1/audio/speech                        在线        instructions
============  ==========================================  ==========  ==============
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from urllib.parse import quote, urlencode

from . import emotion as emotion_mod
from .common import VoiceStudioError

PROVIDERS = {
    "indextts": {
        "label": "IndexTTS（本地 Gradio）",
        "kind": "local",
        "needs_ref": True,
        "supports_emotion": "vector",
        "default_format": "wav",
    },
    "gptsovits": {
        "label": "GPT-SoVITS（本地 api_v2）",
        "kind": "local",
        "needs_ref": True,
        "supports_emotion": "none",
        "default_format": "wav",
    },
    "siliconflow": {
        "label": "硅基流动 SiliconFlow",
        "kind": "online",
        "needs_ref": False,
        "supports_emotion": "prompt",
        "default_format": "wav",
    },
    "minimax": {
        "label": "MiniMax",
        "kind": "online",
        "needs_ref": False,
        "supports_emotion": "enum",
        "default_format": "mp3",
    },
    "mimo": {
        "label": "小米 MiMo",
        "kind": "online",
        "needs_ref": False,
        "supports_emotion": "prompt",
        "default_format": "wav",
    },
    "openai": {
        "label": "OpenAI 兼容 /v1/audio/speech",
        "kind": "online",
        "needs_ref": False,
        "supports_emotion": "prompt",
        "default_format": "mp3",
    },
}

PROVIDER_IDS = tuple(PROVIDERS)

_FORMATS = {"wav", "mp3", "opus", "flac", "aac", "pcm"}

#: IndexTTS 的情感控制方式（Gradio 下拉框的两个取值）
INDEX_EMO_SAME = "与音色参考音频相同"
INDEX_EMO_VECTOR = "使用情感向量控制"


def label_of(provider: str) -> str:
    meta = PROVIDERS.get(provider)
    return str(meta["label"]) if meta else provider


def is_local(provider: str) -> bool:
    return bool(PROVIDERS.get(provider, {}).get("kind") == "local")


def needs_ref(provider: str) -> bool:
    return bool(PROVIDERS.get(provider, {}).get("needs_ref"))


def resolve_format(provider: str, cfg: dict | None = None) -> str:
    meta = PROVIDERS.get(provider, {})
    default = str(meta.get("default_format") or "wav").lower()
    if provider == "indextts":
        return "wav"
    raw = str((cfg or {}).get("format") or "").strip().lower().lstrip(".")
    if not raw:
        return default
    if raw not in _FORMATS:
        raise VoiceStudioError(
            f"提供商 {provider} 的 format 配置「{raw}」不支持，可选："
            + " / ".join(sorted(_FORMATS))
        )
    return raw


# ---------------------------------------------------------------------------
# 通用 HTTP 小工具
# ---------------------------------------------------------------------------


def _brief(text: object, size: int = 300) -> str:
    value = str(text or "").strip().replace("\n", " ")
    return value[:size] + ("…" if len(value) > size else "")


def _response_text(response) -> str:
    try:
        text = response.text
        if text:
            return str(text)
    except Exception:  # noqa: BLE001
        pass
    try:
        content = response.content
        if isinstance(content, bytes):
            return content.decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _check_response(provider: str, url: str, response) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if status >= 400:
        raise VoiceStudioError(
            f"[{provider}] {url} 返回 HTTP {status}：{_brief(_response_text(response), 240)}"
        )


async def _request(client, method: str, url: str, *, provider: str, timeout: float, **kwargs):
    try:
        return await client.request(method, url, timeout=timeout, **kwargs)
    except VoiceStudioError:
        raise
    except Exception as exc:  # noqa: BLE001 - 网络错误统一转中文
        reason = str(exc).strip() or exc.__class__.__name__
        raise VoiceStudioError(f"[{provider}] 请求 {url} 失败：{reason}") from exc


async def _request_bytes(client, method: str, url: str, *, provider: str, timeout: float, **kwargs) -> bytes:
    response = await _request(client, method, url, provider=provider, timeout=timeout, **kwargs)
    _check_response(provider, url, response)
    content = getattr(response, "content", b"")
    if not isinstance(content, bytes) or not content:
        raise VoiceStudioError(
            f"[{provider}] {url} 返回了空音频：{_brief(_response_text(response), 200)}"
        )
    return content


async def _request_json(client, method: str, url: str, *, provider: str, timeout: float, **kwargs) -> dict:
    response = await _request(client, method, url, provider=provider, timeout=timeout, **kwargs)
    _check_response(provider, url, response)
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise VoiceStudioError(
            f"[{provider}] {url} 返回的不是 JSON：{_brief(_response_text(response), 200)}"
        ) from exc
    if not isinstance(data, dict):
        raise VoiceStudioError(f"[{provider}] {url} 返回的 JSON 不是对象：{_brief(data, 200)}")
    return data


def _write_bytes(dest: Path, content: bytes) -> Path:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    if dest.stat().st_size <= 0:
        raise VoiceStudioError(f"提供商返回了空音频（{dest.name}）")
    return dest


# ---------------------------------------------------------------------------
# Gradio（IndexTTS 使用）
# ---------------------------------------------------------------------------


async def gradio_upload(client, base_url: str, file_path: Path, *, timeout: float) -> dict:
    """上传参考音频到 Gradio，返回 FileData dict。"""

    base = base_url.rstrip("/")
    data = Path(file_path).read_bytes()
    filename = Path(file_path).name
    attempts = [f"{base}/gradio_api/upload", f"{base}/upload"]
    last_error = ""
    for url in attempts:
        try:
            response = await client.request(
                "POST",
                url,
                files={"files": (filename, data, "application/octet-stream")},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc).strip() or exc.__class__.__name__
            continue
        if int(getattr(response, "status_code", 0) or 0) >= 400:
            last_error = f"HTTP {response.status_code}: {_brief(_response_text(response), 200)}"
            continue
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            last_error = f"无法解析上传响应：{_brief(_response_text(response), 200)}"
            continue
        server_path = parsed[0] if isinstance(parsed, list) and parsed else parsed
        if isinstance(server_path, str) and server_path:
            return {"path": server_path, "meta": {"_type": "gradio.FileData"}, "orig_name": filename}
        last_error = f"上传响应里没有路径：{_brief(parsed, 200)}"
    raise VoiceStudioError(f"[indextts] 上传参考音频失败：{last_error}")


def parse_sse_complete(text: str) -> tuple[object | None, str | None]:
    """解析 Gradio SSE 文本，返回 (complete 数据, error 文本)。"""

    event = ""
    complete: object | None = None
    failure: str | None = None
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):].strip()
            if event == "complete":
                try:
                    complete = json.loads(payload)
                except (TypeError, ValueError):
                    complete = payload
            elif event == "error":
                failure = payload
    return complete, failure


async def gradio_call(client, base_url: str, fn_name: str, data: list, *, timeout: float) -> object:
    """调用一个 Gradio 命名端点并等待 complete 事件。"""

    base = base_url.rstrip("/")
    prefixes = [f"{base}/gradio_api/call/{fn_name}", f"{base}/call/{fn_name}"]
    last_error = ""
    for prefix in prefixes:
        try:
            start = await client.request(
                "POST",
                prefix,
                json={"data": data},
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc).strip() or exc.__class__.__name__
            continue
        if int(getattr(start, "status_code", 0) or 0) >= 400:
            last_error = f"HTTP {start.status_code}: {_brief(_response_text(start), 200)}"
            continue
        try:
            event_id = start.json().get("event_id")
        except Exception:  # noqa: BLE001
            event_id = None
        if not event_id:
            last_error = f"调用响应里没有 event_id：{_brief(_response_text(start), 200)}"
            continue
        try:
            stream = await client.request("GET", f"{prefix}/{event_id}", timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc).strip() or exc.__class__.__name__
            continue
        if int(getattr(stream, "status_code", 0) or 0) >= 400:
            raise VoiceStudioError(
                f"[indextts] Gradio 事件流 HTTP {stream.status_code}：{_brief(_response_text(stream), 200)}"
            )
        complete, failure = parse_sse_complete(_response_text(stream))
        if failure:
            raise VoiceStudioError(f"[indextts] Gradio 端点报错：{_brief(failure, 400)}")
        if complete is None:
            last_error = "没有收到 complete 事件"
            continue
        return complete
    raise VoiceStudioError(f"[indextts] 调用 Gradio {fn_name} 失败：{last_error}")


def gradio_file_path(result: object) -> str:
    """从 Gradio 各种输出形状里取文件路径 / URL。

    Gradio 5 的 complete 事件可能是 ``[{"path": ..., "url": ...}]``、
    ``{"data": [{"path": ...}]}`` 或直接一个字符串，这里都兼容。
    """

    if isinstance(result, dict) and "data" in result:
        result = result.get("data")
    if isinstance(result, dict) and "value" in result and "data" not in result:
        # 避免把 {"value": ...} 当成 data 列表的最外层
        pass
    first = result[0] if isinstance(result, list) and result else result
    if isinstance(first, str):
        return first
    if isinstance(first, dict):
        for key in ("path", "url"):
            if isinstance(first.get(key), str) and first[key]:
                return first[key]
        value = first.get("value")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("path", "url"):
                if isinstance(value.get(key), str) and value[key]:
                    return value[key]
        if isinstance(value, list) and value:
            return gradio_file_path(value)
    if isinstance(result, list):
        # [{"name": "x", "data": ...}] 形状：继续翻一层
        for item in result:
            found = gradio_file_path(item)
            if found:
                return found
    return ""


async def fetch_gradio_file(client, base_url: str, server_path: str, dest: Path, *, timeout: float) -> Path:
    """Gradio 可能返回服务器本地路径、http URL 或 /gradio_api/file= 相对地址。"""

    dest = Path(dest)
    target = str(server_path or "").strip()
    if target and os.path.exists(target):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
        return dest
    base = base_url.rstrip("/")
    if target.startswith("http://") or target.startswith("https://"):
        url = target
    elif target.startswith(("/gradio_api/", "/file=")):
        url = f"{base}{target}"
    else:
        # Gradio 5 的下载地址：{base}/gradio_api/file=<urlencode(服务器路径)>
        url = f"{base}/gradio_api/file={quote(target, safe='')}"
    content = await _request_bytes(client, "GET", url, provider="indextts", timeout=timeout)
    return _write_bytes(dest, content)


# ---------------------------------------------------------------------------
# 各提供商
# ---------------------------------------------------------------------------


async def _synth_indextts(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    if ref_path is None:
        raise VoiceStudioError("[indextts] 本地提供商需要参考音频：请先 /音色导入")
    base = str(cfg.get("baseUrl") or "http://127.0.0.1:7860")
    file_data = await gradio_upload(client, base, Path(ref_path), timeout=min(timeout, 300.0))
    vector = emotion_mod.to_vector(emotion)
    use_vector = any(value > 0 for value in vector)
    data = [
        INDEX_EMO_VECTOR if use_vector else INDEX_EMO_SAME,
        file_data,
        text,
        str(cfg.get("lang") or "ZH"),
        None,
        float(cfg.get("emoWeight", 0.8) or 0.8),
        *vector,
        "",
        False,
        120,
        float(cfg.get("durationFactor", 1.0) or 1.0),
        True,
        0.8,
        30,
        0.8,
        0.0,
        3,
        10.0,
        1500,
    ]
    result = await gradio_call(client, base, "gen_single", data, timeout=min(timeout, 900.0))
    server_path = gradio_file_path(result)
    if not server_path:
        raise VoiceStudioError(
            f"[indextts] Gradio 没有返回音频路径：{_brief(json.dumps(result, ensure_ascii=False), 300)}"
        )
    await fetch_gradio_file(client, base, server_path, dest, timeout=min(timeout, 300.0))
    return {"emotionApplied": use_vector}


async def _synth_gptsovits(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    if ref_path is None:
        raise VoiceStudioError("[gptsovits] 本地提供商需要参考音频：请先 /音色导入")
    base = str(cfg.get("baseUrl") or "http://127.0.0.1:9880").rstrip("/")
    prompt_text = str(cfg.get("promptText") or "")
    params = {
        "text": text,
        "text_lang": str(cfg.get("textLang") or "zh"),
        "ref_audio_path": str(ref_path),
        "prompt_text": prompt_text,
        "prompt_lang": str(cfg.get("promptLang") or "zh"),
        "top_k": "5",
        "top_p": "1",
        "temperature": "1",
        "text_split_method": "cut5",
        "batch_size": "1",
        "speed_factor": str(speed or cfg.get("speed") or 1),
        "ref_free": "false" if prompt_text else "true",
        "media_type": "wav",
        "streaming_mode": "false",
    }
    url = f"{base}/tts?{urlencode(params)}"
    try:
        content = await _request_bytes(
            client, "GET", url, provider="gptsovits", timeout=min(timeout, 300.0)
        )
    except VoiceStudioError as exc:
        # WebUI 端口没有 /tts 时通常是 404/405，改用 POST JSON 再试一次
        if "HTTP 404" not in str(exc) and "HTTP 405" not in str(exc):
            raise VoiceStudioError(
                f"[gptsovits] 请求失败（{_brief(exc, 200)}）。"
                "GPT-SoVITS 的 WebUI 端口没有 TTS API，请先启动 api_v2.py（默认 9880），"
                "并让 baseUrl 指向它。"
            ) from exc
        content = await _request_bytes(
            client,
            "POST",
            f"{base}/tts",
            provider="gptsovits",
            timeout=min(timeout, 300.0),
            json=params,
        )
    _write_bytes(dest, content)
    return {"emotionApplied": False}


async def _synth_siliconflow(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    api_key = str(cfg.get("apiKey") or "").strip()
    if not api_key:
        raise VoiceStudioError("[siliconflow] apiKey 未配置（配置 → siliconflow → apiKey）")
    prefix = emotion_mod.to_cosy_prefix(emotion)
    payload = {
        "model": str(cfg.get("model") or "FunAudioLLM/CosyVoice2-0.5B"),
        "input": f"{prefix} {text}" if prefix else text,
        "voice": str(cfg.get("voice") or ""),
        "response_format": str(cfg.get("format") or "wav"),
        "speed": float(speed or cfg.get("speed") or 1),
    }
    base = str(cfg.get("baseUrl") or "https://api.siliconflow.cn/v1").rstrip("/")
    content = await _request_bytes(
        client,
        "POST",
        f"{base}/audio/speech",
        provider="siliconflow",
        timeout=min(timeout, 180.0),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    _write_bytes(dest, content)
    return {"emotionApplied": bool(prefix)}


async def _synth_minimax(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    api_key = str(cfg.get("apiKey") or "").strip()
    if not api_key:
        raise VoiceStudioError("[minimax] apiKey 未配置（配置 → minimax → apiKey）")
    body = {
        "model": str(cfg.get("model") or "speech-2.8-hd"),
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": str(cfg.get("voice") or "female-shaonv"),
            "speed": float(speed or cfg.get("speed") or 1),
            "vol": 1,
            "pitch": 0,
        },
        "audio_setting": {
            "format": str(cfg.get("format") or "mp3"),
            "sample_rate": 32000,
            "bitrate": 128000,
            "channel": 1,
        },
        "output_format": "hex",
    }
    emotion_enum = emotion_mod.to_minimax(emotion)
    if emotion_enum:
        body["voice_setting"]["emotion"] = emotion_enum
    base = str(cfg.get("baseUrl") or "https://api.minimax.chat/v1").rstrip("/")
    url = f"{base}/t2a_v2"
    group_id = str(cfg.get("groupId") or "").strip()
    if group_id:
        url = f"{url}?{urlencode({'GroupId': group_id})}"
    data = await _request_json(
        client,
        "POST",
        url,
        provider="minimax",
        timeout=min(timeout, 180.0),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
    )
    base_resp = data.get("base_resp") if isinstance(data.get("base_resp"), dict) else {}
    status_code = base_resp.get("status_code")
    if status_code is not None and int(status_code) != 0:
        raise VoiceStudioError(
            f"[minimax] base_resp.status_code={status_code}: {_brief(base_resp.get('status_msg'), 200)}"
        )
    audio = (data.get("data") or {}).get("audio") if isinstance(data.get("data"), dict) else None
    if not audio:
        raise VoiceStudioError(
            f"[minimax] 响应里没有 data.audio：{_brief(json.dumps(data, ensure_ascii=False), 300)}"
        )
    audio = str(audio)
    if audio.startswith("http://") or audio.startswith("https://"):
        content = await _request_bytes(
            client, "GET", audio, provider="minimax", timeout=min(timeout, 180.0)
        )
        _write_bytes(dest, content)
    elif all(ch in "0123456789abcdefABCDEF" for ch in audio):
        _write_bytes(dest, bytes.fromhex(audio))
    else:
        import base64

        _write_bytes(dest, base64.b64decode(audio))
    return {"emotionApplied": bool(emotion_enum)}


async def _synth_mimo(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    api_key = str(cfg.get("apiKey") or "").strip()
    if not api_key:
        raise VoiceStudioError("[mimo] apiKey 未配置（配置 → mimo → apiKey）")
    style = emotion_mod.to_mimo_style(emotion) or str(cfg.get("style") or "")
    messages: list[dict] = []
    if style:
        messages.append({"role": "user", "content": style})
    messages.append({"role": "assistant", "content": text})
    body = {
        "model": str(cfg.get("model") or "mimo-v2.5-tts"),
        "messages": messages,
        "audio": {"format": str(cfg.get("format") or "wav"), "voice": str(cfg.get("voice") or "")},
    }
    if speed and float(speed) != 1:
        body["audio"]["speed"] = float(speed)
    base = str(cfg.get("baseUrl") or "").rstrip("/")
    if not base:
        raise VoiceStudioError("[mimo] baseUrl 未配置（配置 → mimo → baseUrl）")
    data = await _request_json(
        client,
        "POST",
        f"{base}/chat/completions",
        provider="mimo",
        timeout=min(timeout, 180.0),
        headers={
            "api-key": api_key,
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
    )
    audio = None
    try:
        audio = data["choices"][0]["message"]["audio"]["data"]
    except (KeyError, IndexError, TypeError):
        audio = None
    if not audio:
        raise VoiceStudioError(
            f"[mimo] 响应里没有 choices[0].message.audio.data："
            f"{_brief(json.dumps(data, ensure_ascii=False), 300)}"
        )
    import base64

    _write_bytes(dest, base64.b64decode(str(audio)))
    return {"emotionApplied": bool(style)}


async def _synth_openai(client, cfg: dict, *, text, ref_path, emotion, speed, dest, timeout):
    api_key = str(cfg.get("apiKey") or "").strip()
    if not api_key:
        raise VoiceStudioError("[openai] apiKey 未配置（配置 → openai → apiKey）")
    payload = {
        "model": str(cfg.get("model") or "gpt-4o-mini-tts"),
        "input": text,
        "voice": str(cfg.get("voice") or "alloy"),
        "response_format": str(cfg.get("format") or "mp3"),
        "speed": float(speed or cfg.get("speed") or 1),
    }
    instruction = ""
    if cfg.get("emotionInstructions", True) is not False:
        instruction = emotion_mod.to_cosy_prefix(emotion).replace(" <|endofprompt|>", "")
    if instruction:
        payload["instructions"] = instruction
    base = str(cfg.get("baseUrl") or "https://api.openai.com/v1").rstrip("/")
    content = await _request_bytes(
        client,
        "POST",
        f"{base}/audio/speech",
        provider="openai",
        timeout=min(timeout, 180.0),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
    )
    _write_bytes(dest, content)
    return {"emotionApplied": bool(instruction)}


_ADAPTERS = {
    "indextts": _synth_indextts,
    "gptsovits": _synth_gptsovits,
    "siliconflow": _synth_siliconflow,
    "minimax": _synth_minimax,
    "mimo": _synth_mimo,
    "openai": _synth_openai,
}


async def synthesize(
    client,
    provider: str,
    cfg: dict | None,
    *,
    text: str,
    ref_path: Path | None,
    emotion: dict | None,
    speed: float | None,
    dest: Path,
    timeout: float = 600.0,
) -> dict:
    """合成一段语音，返回 ``{provider, path, format, emotionApplied}``。

    Args:
        client: 异步 httpx.AsyncClient（由调用方持有）。
        provider: PROVIDERS 里的 id。
        cfg: 该提供商的配置子对象。
        text: 待合成文本。
        ref_path: 本地提供商需要的参考音频路径。
        emotion: ``emotion.resolve_emotion`` 的结果。
        speed: 语速倍率（1.0 为原速）。
        dest: 输出文件路径（后缀会按真实格式修正）。
    """

    provider = str(provider or "").strip()
    adapter = _ADAPTERS.get(provider)
    if adapter is None:
        raise VoiceStudioError(
            f"未知的 TTS 提供商「{provider}」，可选：" + " / ".join(PROVIDER_IDS)
        )
    cfg = dict(cfg or {})
    body = str(text or "").strip()
    if not body:
        raise VoiceStudioError("待朗读文本为空。")

    fmt = resolve_format(provider, cfg)
    dest = Path(dest)
    if dest.suffix.lower() != f".{fmt}":
        dest = dest.with_suffix(f".{fmt}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    result = await adapter(
        client,
        cfg,
        text=body,
        ref_path=ref_path,
        emotion=emotion,
        speed=float(speed) if speed else None,
        dest=dest,
        timeout=float(timeout),
    )
    if not dest.is_file() or dest.stat().st_size <= 0:
        raise VoiceStudioError(f"[{provider}] 合成结束但没有产出音频文件：{dest}")
    return {
        "provider": provider,
        "path": dest,
        "format": fmt,
        "emotionApplied": bool((result or {}).get("emotionApplied")),
    }


__all__ = [
    "PROVIDERS",
    "PROVIDER_IDS",
    "fetch_gradio_file",
    "gradio_call",
    "gradio_file_path",
    "gradio_upload",
    "is_local",
    "label_of",
    "needs_ref",
    "parse_sse_complete",
    "resolve_format",
    "synthesize",
]
