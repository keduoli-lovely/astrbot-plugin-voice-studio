"""语音工坊 AstrBot 插件（MVP）。

把 DSH「语音工坊」的语音能力接进 AstrBot（OneBot v11 / aiocqhttp）：

* ``/朗读 <文本>``（别名 ``/语音说``）
    -> ``POST {voiceStudioUrl}/tts-studio/api/tts``
    -> 拿到音频文件后用 AstrBot 的 ``Record`` 消息段发出语音条。
* ``/翻唱 <歌曲绝对路径> [模型]``
    -> ``rvc-worker``: ``POST /v1/files`` 上传 -> ``/v1/jobs`` 人声分离
    -> ``/v1/jobs`` RVC 换声 -> 下载 vocal / instrumental / converted
    -> 插件内 numpy 混音（RMS 对齐 + 峰值归一化，逻辑移植自
       ``voice-studio/lib/wav.js`` 的 ``mixCover``）
    -> 发送语音条。
* ``/音色``      -> ``GET {voiceStudioUrl}/tts-studio/api/voices``
* ``/语音状态``  -> ``GET {voiceStudioUrl}/tts-studio/api/state`` + ``GET {workerUrl}/v1/health``

本文件顶部「已核实的 AstrBot API」一节列出了从官方文档/源码确认过的导入路径，
README.md 里也写明了文档 URL，避免后续维护者凭记忆猜 API。

依赖：httpx（所有 HTTP，asyncio 友好）、numpy（仅翻唱混音，可流式下载、不阻塞事件循环）。
不引入 torch / RVC / soundfile，全部重活都在 HTTP 另一端的服务上。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

# ---------------------------------------------------------------------------
# 已核实的 AstrBot API（2026-11，对照 docs.astrbot.app 与 AstrBot master 源码）
#
#   1) 插件基类 / Context:
#        from astrbot.api.star import Context, Star
#        class X(Star): def __init__(self, context: Context, config: AstrBotConfig)
#   2) 事件与过滤器:
#        from astrbot.api.event import filter, AstrMessageEvent
#        @filter.command("朗读", alias={"语音说"})
#        async def handler(self, event: AstrMessageEvent): yield event.plain_result("...")
#   3) 富媒体（语音条）:
#        from astrbot.api.message_components import Record
#        Record.fromFileSystem(path)  -> file=file:///... + path=...
#        yield event.chain_result([record])
#      （aiocqhttp 适配器会把 Record 统一转成 wav base64 再交给 OneBot，见 README）
#   4) 配置:
#        _conf_schema.json + __init__(self, context, config: AstrBotConfig)
#        from astrbot.api import AstrBotConfig, logger
# ---------------------------------------------------------------------------

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Record
from astrbot.api.star import Context, Star

PLUGIN_NAME = "astrbot_plugin_voice_studio"
PLUGIN_VERSION = "0.1.0"

#: voice-studio 的路由前缀（index.js: `const BASE = '/tts-studio'`）
VOICE_PREFIX = "/tts-studio"
TTS_PATH = f"{VOICE_PREFIX}/api/tts"
VOICES_PATH = f"{VOICE_PREFIX}/api/voices"
STATE_PATH = f"{VOICE_PREFIX}/api/state"

AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".amr"}
COMMAND_NAMES = ("语音状态", "语音说", "朗读", "翻唱", "音色")

_TMP_ROOT = Path(tempfile.gettempdir()) / PLUGIN_NAME


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


class VoiceStudioError(RuntimeError):
    """所有可展示给用户的中文错误都用这个类型，消息本身就是给用户看的。"""


def _load_httpx():
    """延迟导入 httpx：依赖缺失时插件仍能加载，只在真正发请求时给中文提示。"""

    try:
        import httpx  # noqa: PLC0415  (延迟导入是有意的)
    except ImportError as exc:  # pragma: no cover - 依赖问题
        raise VoiceStudioError(
            "缺少 httpx 依赖：请在 AstrBot 插件管理里安装本插件的 requirements.txt，"
            "或手动执行 pip install httpx。"
        ) from exc
    return httpx


def _conf_get(config: Any, key: str, default: Any = None) -> Any:
    if not config:
        return default
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001 - config 可能是任意 dict 子类
        return default
    return default if value is None else value


def _conf_str(config: Any, key: str, default: str = "") -> str:
    value = _conf_get(config, key, default)
    if value is None:
        return default
    return str(value).strip()


def _conf_int(config: Any, key: str, default: int = 0) -> int:
    value = _conf_get(config, key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _conf_bool(config: Any, key: str, default: bool = False) -> bool:
    value = _conf_get(config, key, default)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "t"}


def _conf_list(config: Any, key: str, default: list | None = None) -> list:
    value = _conf_get(config, key, default if default is not None else [])
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _message_text(event: AstrMessageEvent) -> str:
    """取事件纯文本；不同 AstrBot 版本有属性/方法两种形态，这里都兼容。"""

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


def _strip_wake_prefix(text: str) -> str:
    return re.sub(r"^[/!！。]+", "", str(text or "").strip()).strip()


def _command_arg(message_str: str) -> str:
    """去掉唤醒前缀与命令名，返回命令后面的原始文本（保留内部空格）。

    AstrBot 的 CommandFilter 只按空格拆参数，``/朗读 你好 世界`` 用 ``str`` 注解
    只会拿到 "你好"；所以这里自己解析 ``event.message_str``。
    """

    text = _strip_wake_prefix(message_str)
    for name in sorted(COMMAND_NAMES, key=len, reverse=True):
        if text == name:
            return ""
        if text.startswith(name + " ") or text.startswith(name + "\u3000"):
            return text[len(name):].strip()
    return text


def _split_cover_arg(text: str) -> tuple[str, str]:
    """解析 ``<歌曲路径> [模型]``；支持用引号包住带空格的路径。"""

    text = str(text or "").strip()
    if not text:
        return "", ""
    if text[0] in {'"', "'"}:
        quote_char = text[0]
        end = text.find(quote_char, 1)
        if end == -1:
            body = text[1:].strip()
            parts = body.split(None, 1)
            return parts[0] if parts else "", parts[1].strip() if len(parts) > 1 else ""
        song = text[1:end].strip()
        rest = text[end + 1:].strip()
        parts = rest.split(None, 1)
        return song, parts[0] if parts else ""
    parts = text.split(None, 1)
    song = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    model = rest.split(None, 1)[0] if rest else ""
    return song, model


def _format_seconds(value: float | int | None) -> str:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if seconds < 0:
        seconds = 0
    minutes, sec = divmod(int(round(seconds)), 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def _record_for(path: Path) -> Record:
    """构造语音消息段；``fromFileSystem`` 在本机文件场景最稳。"""

    try:
        return Record.fromFileSystem(str(path))
    except AttributeError:  # 兼容极老版本：显式给 file/url
        return Record(file=str(path), url=str(path))


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe（可选能力，找不到就降级，不报错）
# ---------------------------------------------------------------------------


def _which(name: str) -> str | None:
    try:
        return shutil.which(name)
    except Exception:  # noqa: BLE001
        return None


async def _run_ffmpeg(args: list[str], timeout: float = 1800.0) -> bool:
    binary = _which("ffmpeg")
    if not binary:
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    except Exception:  # noqa: BLE001
        return False
    return proc.returncode == 0


async def _probe_duration(path: Path) -> float | None:
    """用 ffprobe 探测时长；没有 ffprobe 时返回 None（不做硬限制）。"""

    binary = _which("ffprobe")
    if not binary:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
    except Exception:  # noqa: BLE001
        return None
    try:
        return float(out.decode("utf-8", "ignore").strip())
    except (TypeError, ValueError):
        return None


async def _ensure_wav(path: Path, work_dir: Path) -> Path:
    """AstrBot 文档标注语音段建议 wav；非 wav 且本机有 ffmpeg 时转成 wav。"""

    if path.suffix.lower() == ".wav":
        return path
    if not _which("ffmpeg"):
        return path
    out = work_dir / f"{path.stem}_{uuid.uuid4().hex[:6]}.wav"
    ok = await _run_ffmpeg(["-y", "-i", str(path), "-ar", "44100", "-ac", "2", str(out)])
    if ok and out.is_file() and out.stat().st_size > 0:
        return out
    return path


# ---------------------------------------------------------------------------
# WAV 编解码 / 混音（移植自 voice-studio/lib/wav.js 的 mixCover）
# ---------------------------------------------------------------------------


def _load_numpy():
    try:
        import numpy as np  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - 依赖问题
        raise VoiceStudioError(
            "翻唱混音需要 numpy：请安装本插件的 requirements.txt（pip install numpy）。"
        ) from exc
    return np


def _wav_duration_sec(path: Path) -> float | None:
    """只读 WAV 头计算时长，不加载采样。"""

    try:
        file_size = Path(path).stat().st_size
    except OSError:
        return None
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"RIFF":
                return None
            fh.read(4)
            if fh.read(4) != b"WAVE":
                return None
            rate = channels = bits = 0
            while True:
                chunk_id = fh.read(4)
                if len(chunk_id) < 4:
                    return None
                raw_size = fh.read(4)
                if len(raw_size) < 4:
                    return None
                size = int.from_bytes(raw_size, "little")
                if chunk_id == b"fmt ":
                    body = fh.read(size)
                    if len(body) < 16:
                        return None
                    channels = int.from_bytes(body[2:4], "little")
                    rate = int.from_bytes(body[4:8], "little")
                    bits = int.from_bytes(body[14:16], "little")
                    if size % 2:
                        fh.seek(1, 1)
                elif chunk_id == b"data":
                    # 0xFFFFFFFF 之类的流式 wav 头按文件剩余字节收敛
                    available = max(0, file_size - fh.tell())
                    usable = min(size, available)
                    block = channels * (bits // 8)
                    if rate and block:
                        return usable / block / rate
                    return None
                else:
                    fh.seek(size + (size & 1), 1)
    except OSError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _decode_wav(path: Path) -> dict:
    """解码 PCM / IEEE float WAV 成 float64 交错采样。"""

    np = _load_numpy()
    raw = Path(path).read_bytes()
    if len(raw) < 12 or raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise VoiceStudioError(f"{Path(path).name} 不是有效的 WAV 文件")
    pos = 12
    fmt: tuple[int, int, int, int] | None = None
    data_offset = None
    data_size = 0
    while pos + 8 <= len(raw):
        chunk_id = raw[pos:pos + 4]
        size = int.from_bytes(raw[pos + 4:pos + 8], "little")
        body = pos + 8
        if chunk_id == b"fmt ":
            fmt_tag = int.from_bytes(raw[body:body + 2], "little")
            channels = int.from_bytes(raw[body + 2:body + 4], "little")
            rate = int.from_bytes(raw[body + 4:body + 8], "little")
            bits = int.from_bytes(raw[body + 14:body + 16], "little")
            if fmt_tag == 0xFFFE and size >= 40:  # WAVE_FORMAT_EXTENSIBLE
                fmt_tag = int.from_bytes(raw[body + 24:body + 26], "little")
            fmt = (fmt_tag, channels, rate, bits)
        elif chunk_id == b"data":
            data_offset = body
            data_size = min(size, max(0, len(raw) - body))
            break
        pos = body + size + (size & 1)
    if not fmt or data_offset is None:
        raise VoiceStudioError(f"{Path(path).name} 缺少 WAV fmt/data 块")
    fmt_tag, channels, rate, bits = fmt
    if channels < 1 or rate < 1:
        raise VoiceStudioError(f"{Path(path).name} 的 WAV 头无效（channels={channels}, rate={rate}）")
    bytes_per = max(1, bits // 8)
    frames = data_size // (bytes_per * channels)
    count = frames * channels
    if count <= 0:
        raise VoiceStudioError(f"{Path(path).name} 是空音频")
    buf = raw[data_offset:data_offset + count * bytes_per]
    if fmt_tag == 3 and bits == 32:
        samples = np.frombuffer(buf, dtype="<f4", count=count).astype(np.float64)
    elif fmt_tag == 3 and bits == 64:
        samples = np.frombuffer(buf, dtype="<f8", count=count).astype(np.float64)
    elif fmt_tag == 1 and bits == 8:
        samples = (np.frombuffer(buf, dtype=np.uint8, count=count).astype(np.float64) - 128.0) / 128.0
    elif fmt_tag == 1 and bits == 16:
        samples = np.frombuffer(buf, dtype="<i2", count=count).astype(np.float64) / 32768.0
    elif fmt_tag == 1 and bits == 24:
        packed = np.frombuffer(buf, dtype=np.uint8, count=count * 3).reshape(-1, 3).astype(np.int32)
        ints = packed[:, 0] | (packed[:, 1] << 8) | (packed[:, 2] << 16)
        ints = np.where(ints >= 0x800000, ints - 0x1000000, ints)
        samples = ints.astype(np.float64) / 8388608.0
    elif fmt_tag == 1 and bits == 32:
        samples = np.frombuffer(buf, dtype="<i4", count=count).astype(np.float64) / 2147483648.0
    else:
        raise VoiceStudioError(f"不支持的 WAV 编码（format={fmt_tag}, bits={bits}）")
    return {"sample_rate": rate, "channels": channels, "bits": bits, "samples": samples}


def _to_mono(decoded: dict):
    np = _load_numpy()
    samples = decoded["samples"]
    channels = int(decoded["channels"])
    if channels == 1:
        return samples
    frames = samples.size // channels
    return samples[: frames * channels].reshape(frames, channels).mean(axis=1)


def _resample(decoded: dict, target_rate: int) -> dict:
    np = _load_numpy()
    source_rate = int(decoded["sample_rate"])
    channels = int(decoded["channels"])
    if source_rate == target_rate:
        return decoded
    samples = decoded["samples"]
    frames = samples.size // channels
    if frames <= 1:
        frames = max(1, frames)
        repeated = np.repeat(samples[:channels], 1, axis=0)
        return {"sample_rate": target_rate, "channels": channels, "bits": decoded["bits"], "samples": repeated}
    arr = samples[: frames * channels].reshape(frames, channels)
    out_frames = max(1, int(round(frames * target_rate / source_rate)))
    src_index = np.linspace(0.0, frames - 1.0, out_frames)
    base = np.arange(frames, dtype=np.float64)
    channels_out = [np.interp(src_index, base, arr[:, ch]) for ch in range(channels)]
    out = np.stack(channels_out, axis=1).ravel()
    return {"sample_rate": target_rate, "channels": channels, "bits": decoded["bits"], "samples": out}


def _write_wav24(path: Path, sample_rate: int, channels: int, samples) -> None:
    np = _load_numpy()
    ints = np.clip(np.round(np.asarray(samples, dtype=np.float64) * 8388607.0), -8388608, 8388607)
    packed = ints.astype("<i4").tobytes()
    body = np.frombuffer(packed, dtype=np.uint8).reshape(-1, 4)[:, :3].tobytes()
    block_align = channels * 3
    header = bytearray()
    header += b"RIFF"
    header += int(36 + len(body)).to_bytes(4, "little")
    header += b"WAVE"
    header += b"fmt "
    header += (16).to_bytes(4, "little")
    header += (1).to_bytes(2, "little")
    header += channels.to_bytes(2, "little")
    header += int(sample_rate).to_bytes(4, "little")
    header += (sample_rate * block_align).to_bytes(4, "little")
    header += block_align.to_bytes(2, "little")
    header += (24).to_bytes(2, "little")
    header += b"data"
    header += len(body).to_bytes(4, "little")
    Path(path).write_bytes(bytes(header) + body)


def _mix_cover(
    instrumental: Path,
    vocal: Path,
    vocal_source: Path | None,
    out_path: Path,
    target_peak: float = 0.891,
) -> Path:
    """把换声后的人声（居中单声道）叠到伴奏上，参考 mixCover：

    1. 人声重采样到伴奏采样率；
    2. 用「换声前的人声」的 RMS 对齐换声后的人声响度（RVC 输出音量常漂移）；
    3. 与立体声伴奏相加；
    4. 峰值归一化到 -1 dBFS（0.891）。
    """

    np = _load_numpy()
    bed = _decode_wav(instrumental)
    voice_decoded = _decode_wav(vocal)
    sample_rate = int(bed["sample_rate"])
    channels = int(bed["channels"])
    if channels < 1:
        raise VoiceStudioError("伴奏声道数无效")

    voice = _to_mono(_resample(voice_decoded, sample_rate))
    bed_samples = bed["samples"].reshape(-1, channels)
    frames = min(bed_samples.shape[0], voice.shape[0])
    if frames <= 0:
        raise VoiceStudioError("混音失败：人声或伴奏为空音频")

    voice = np.asarray(voice[:frames], dtype=np.float64).copy()
    if vocal_source is not None:
        try:
            source = _to_mono(_resample(_decode_wav(vocal_source), sample_rate))
            length = min(frames, source.shape[0])
            if length > 0:
                ref_rms = float(np.sqrt(np.mean(np.square(source[:length]))))
                cur_rms = float(np.sqrt(np.mean(np.square(voice[:length]))))
                if ref_rms > 1e-6 and cur_rms > 1e-6:
                    voice *= ref_rms / cur_rms
        except VoiceStudioError:
            pass  # 参考人声读不了就按原音量混，不影响出片

    mixed = bed_samples[:frames].astype(np.float64) + voice[:, None]
    peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
    if peak > 1e-6:
        mixed *= float(target_peak) / peak
    else:
        mixed.fill(0.0)
    _write_wav24(out_path, sample_rate, channels, mixed.ravel())
    return out_path


# ---------------------------------------------------------------------------
# HTTP 客户端（voice-studio + rvc-worker）
# ---------------------------------------------------------------------------


def _normalize_base(url: str, label: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise VoiceStudioError(f"未配置 {label}，请先在 AstrBot 插件配置里填写。")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    return value.rstrip("/")


def _error_text(url: str, status: int, data: Any) -> str:
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or error.get("detail") or "").strip()
            if code and message:
                return f"{message}（{code}, HTTP {status}）"
            if message:
                return f"{message}（HTTP {status}）"
        if isinstance(error, str) and error.strip():
            return f"{error.strip()}（HTTP {status}）"
        if data.get("message"):
            return f"{data['message']}（HTTP {status}）"
    return f"HTTP {status}（{url}）"


class VoiceStudioClient:
    """语音工坊 / rvc-worker 的异步 HTTP 客户端。

    * 只使用 httpx.AsyncClient，不阻塞事件循环；
    * 下载走 ``client.stream`` 流式落盘，适合整首歌级别的大文件；
    * 所有错误统一抛 :class:`VoiceStudioError`，消息是可直接回给用户的中文。
    """

    def __init__(
        self,
        *,
        voice_base: str,
        voice_token: str = "",
        worker_base: str = "",
        worker_token: str = "",
        read_timeout: float = 600.0,
        job_timeout: float = 1800.0,
        upload_timeout: float = 600.0,
    ) -> None:
        self._voice_base_raw = voice_base
        self._voice_token = voice_token
        self._worker_base_raw = worker_base
        self._worker_token = worker_token
        self.read_timeout = max(30.0, float(read_timeout))
        self.job_timeout = max(60.0, float(job_timeout))
        self.upload_timeout = max(60.0, float(upload_timeout))
        self._client = None
        self._httpx = None

    # -- 基础 ---------------------------------------------------------------
    @property
    def voice_base(self) -> str:
        return _normalize_base(self._voice_base_raw, "voiceStudioUrl")

    @property
    def worker_base(self) -> str:
        return _normalize_base(self._worker_base_raw, "workerUrl")

    def voice_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._voice_token:
            headers["Authorization"] = f"Bearer {self._voice_token}"
        return headers

    def worker_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._worker_token:
            headers["Authorization"] = f"Bearer {self._worker_token}"
        return headers

    async def _get_client(self):
        if self._client is not None:
            return self._client
        httpx = _load_httpx()
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=self.read_timeout,
                write=self.upload_timeout,
                pool=10.0,
            ),
            follow_redirects=True,
        )
        return self._client

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    # -- 低层请求 -----------------------------------------------------------
    async def _api_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict | None = None,
        files: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        client = await self._get_client()
        request_timeout = self.read_timeout if timeout is None else max(5.0, float(timeout))
        try:
            response = await client.request(
                method,
                url,
                headers=headers,
                json=json_body,
                files=files,
                timeout=request_timeout,
            )
        except VoiceStudioError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络错误统一转中文
            reason = str(exc).strip() or exc.__class__.__name__
            raise VoiceStudioError(f"请求 {url} 失败：{reason}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            data = None
        if status >= 400:
            raise VoiceStudioError(_error_text(url, status, data))
        if not isinstance(data, dict):
            raise VoiceStudioError(f"{url} 返回了非 JSON 内容（HTTP {status}）")
        return data

    async def _download(
        self,
        url: str,
        dest: Path,
        *,
        headers: dict[str, str],
        timeout: float | None = None,
    ) -> Path:
        client = await self._get_client()
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        request_timeout = self.read_timeout if timeout is None else max(10.0, float(timeout))
        total = 0
        try:
            async with client.stream(
                "GET",
                url,
                headers=headers,
                timeout=request_timeout,
            ) as response:
                status = int(getattr(response, "status_code", 0) or 0)
                if status >= 400:
                    payload = b""
                    try:
                        payload = await response.aread()
                    except Exception:  # noqa: BLE001
                        payload = b""
                    data = None
                    if payload:
                        try:
                            import json  # noqa: PLC0415

                            data = json.loads(payload.decode("utf-8", "ignore"))
                        except Exception:  # noqa: BLE001
                            data = None
                    raise VoiceStudioError(_error_text(url, status, data))
                with open(dest, "wb") as handle:
                    async for chunk in response.aiter_bytes(65536):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        total += len(chunk)
        except VoiceStudioError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = str(exc).strip() or exc.__class__.__name__
            raise VoiceStudioError(f"下载 {url} 失败：{reason}") from exc
        if total <= 0:
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise VoiceStudioError(f"从 {url} 下载到空文件")
        return dest

    # -- voice-studio -------------------------------------------------------
    def voice_url(self, path_or_url: str) -> str:
        value = str(path_or_url or "").strip()
        if not value:
            raise VoiceStudioError("语音工坊返回了空 url")
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if not value.startswith("/"):
            value = "/" + value
        return f"{self.voice_base}{value}"

    async def tts(
        self,
        text: str,
        *,
        provider: str | None = None,
        voice: str | None = None,
        emotion: str | dict | None = None,
    ) -> dict:
        """调用语音工坊 TTS。

        真实参数名以语音工坊实现为准（``index.js`` -> ``studio.generate``）：
        ``voice`` 在 HTTP 层叫 ``voiceId``，``emotion`` 可以是字符串或
        ``{label, intensity}``。``sendToQQ`` 固定 false，QQ 发送由 AstrBot 自己做。
        """

        body: dict[str, Any] = {"text": str(text or ""), "sendToQQ": False}
        if provider:
            body["provider"] = str(provider)
        if voice:
            body["voiceId"] = str(voice)
        if emotion:
            body["emotion"] = emotion
        url = f"{self.voice_base}{TTS_PATH}"
        data = await self._api_json(
            "POST",
            url,
            headers=self.voice_headers(),
            json_body=body,
            timeout=self.read_timeout,
        )
        if data.get("ok") is False:
            raise VoiceStudioError(str(data.get("error") or "语音工坊合成失败"))
        if not data.get("file") and not data.get("url"):
            raise VoiceStudioError("语音工坊没有返回音频文件路径")
        return data

    async def list_voices(self) -> list[dict]:
        url = f"{self.voice_base}{VOICES_PATH}"
        data = await self._api_json("GET", url, headers=self.voice_headers(), timeout=30.0)
        voices = data.get("voices")
        if not isinstance(voices, list):
            return []
        return [item for item in voices if isinstance(item, dict)]

    async def state(self) -> dict:
        url = f"{self.voice_base}{STATE_PATH}"
        return await self._api_json("GET", url, headers=self.voice_headers(), timeout=15.0)

    async def download_voice_audio(self, url: str, dest: Path) -> Path:
        return await self._download(url, dest, headers=self.voice_headers())

    # -- rvc-worker ---------------------------------------------------------
    async def worker_health(self) -> dict:
        url = f"{self.worker_base}/v1/health"
        return await self._api_json("GET", url, headers=self.worker_headers(), timeout=10.0)

    async def models(self) -> dict:
        url = f"{self.worker_base}/v1/models"
        return await self._api_json("GET", url, headers=self.worker_headers(), timeout=20.0)

    async def upload_file(self, path: Path) -> str:
        """multipart 上传歌曲到 worker，返回 fileId。"""

        url = f"{self.worker_base}/v1/files"
        try:
            handle = open(path, "rb")  # noqa: SIM115 - 需要让 httpx 在请求期间读文件
        except OSError as exc:
            raise VoiceStudioError(f"读取歌曲文件失败：{exc}") from exc
        try:
            with handle:
                data = await self._api_json(
                    "POST",
                    url,
                    headers=self.worker_headers(),
                    files={"file": (Path(path).name, handle, "application/octet-stream")},
                    timeout=self.upload_timeout,
                )
        finally:
            if not handle.closed:
                handle.close()
        file_id = str(data.get("fileId") or "").strip()
        if not file_id:
            raise VoiceStudioError("上传歌曲后 worker 没有返回 fileId")
        return file_id

    async def create_job(self, job_type: str, file_id: str, params: dict | None = None) -> str:
        url = f"{self.worker_base}/v1/jobs"
        payload: dict[str, Any] = {
            "type": job_type,
            "fileId": file_id,
            "params": {**(params or {}), "fileId": file_id},
        }
        data = await self._api_json(
            "POST",
            url,
            headers=self.worker_headers(),
            json_body=payload,
            timeout=60.0,
        )
        job_id = str(data.get("jobId") or data.get("id") or "").strip()
        if not job_id:
            raise VoiceStudioError("worker 没有返回 jobId")
        return job_id

    async def wait_job(self, job_id: str) -> dict:
        """轮询 ``GET /v1/jobs/{id}`` 直到 succeeded / failed / canceled / 超时。"""

        url = f"{self.worker_base}/v1/jobs/{job_id}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.job_timeout
        last_status = ""
        while True:
            data = await self._api_json("GET", url, headers=self.worker_headers(), timeout=20.0)
            status = str(data.get("status") or "").strip().lower()
            if status != last_status:
                logger.debug(
                    "[%s] worker job %s -> %s (stage=%s)",
                    PLUGIN_NAME,
                    job_id,
                    status,
                    data.get("stage"),
                )
                last_status = status
            if status == "succeeded":
                return data
            if status in {"failed", "canceled", "cancelled"}:
                detail = data.get("error") or data.get("stage") or "未知原因"
                raise VoiceStudioError(f"worker 任务失败（{status}）：{detail}")
            if loop.time() > deadline:
                raise VoiceStudioError(
                    f"worker 任务超时（超过 {int(self.job_timeout)} 秒，job {job_id}）"
                )
            await asyncio.sleep(2.0)

    async def download_job_output(self, job_id: str, name: str, dest: Path) -> Path:
        url = f"{self.worker_base}/v1/jobs/{job_id}/output?name={quote(str(name))}"
        return await self._download(url, dest, headers=self.worker_headers())

    async def download_file(self, file_id: str, dest: Path) -> Path:
        url = f"{self.worker_base}/v1/files/{quote(str(file_id))}"
        return await self._download(url, dest, headers=self.worker_headers())


def _pick_output(outputs: list, keywords: tuple[str, ...]) -> dict | None:
    for keyword in keywords:
        for output in outputs:
            if not isinstance(output, dict) or not output.get("fileId"):
                continue
            name = str(output.get("name") or "").lower()
            if keyword in name:
                return output
    return None


# ---------------------------------------------------------------------------
# AstrBot 插件
# ---------------------------------------------------------------------------


class VoiceStudioPlugin(Star):
    """语音工坊插件：/朗读 /翻唱 /音色 /语音状态。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self._tmp = Path(tempfile.gettempdir()) / PLUGIN_NAME
        try:
            self._tmp.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 极端只读环境
            self._tmp = Path(tempfile.mkdtemp(prefix=PLUGIN_NAME))
        self._client: VoiceStudioClient | None = None

    # -- 生命周期 -----------------------------------------------------------
    async def terminate(self) -> None:  # pragma: no cover - AstrBot 卸载时调用
        if self._client is not None:
            await self._client.close()
            self._client = None

    def _build_client(self) -> VoiceStudioClient:
        return VoiceStudioClient(
            voice_base=_conf_str(self.config, "voiceStudioUrl", ""),
            voice_token=_conf_str(self.config, "voiceStudioToken", ""),
            worker_base=_conf_str(self.config, "workerUrl", ""),
            worker_token=_conf_str(self.config, "workerToken", ""),
            read_timeout=float(max(30, _conf_int(self.config, "requestTimeoutSec", 600))),
            job_timeout=float(max(60, _conf_int(self.config, "jobTimeoutSec", 1800))),
        )

    async def _get_vs_client(self) -> VoiceStudioClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- 杂项 ---------------------------------------------------------------
    def _check_allowed(self, event: AstrMessageEvent) -> str | None:
        gid = _group_id(event)
        if not gid:
            return None  # 私聊不限制
        allowed = [str(item).strip() for item in _conf_list(self.config, "allowedGroups", []) if str(item).strip()]
        if not allowed:
            return None
        if gid in allowed:
            return None
        return f"⛔ 本群（{gid}）不在语音工坊白名单里，管理员可在插件配置 allowedGroups 中添加。"

    def _new_tmp(self, suffix: str) -> Path:
        return self._tmp / f"{int(time.time())}_{uuid.uuid4().hex[:8]}{suffix}"

    def _new_tmp_dir(self) -> Path:
        path = self._tmp / f"job_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_tmp(self, max_age_sec: float = 24 * 3600) -> None:
        now = time.time()
        try:
            entries = list(self._tmp.iterdir())
        except OSError:
            return
        for entry in entries:
            try:
                if entry.is_file() and now - entry.stat().st_mtime > max_age_sec:
                    entry.unlink(missing_ok=True)
                elif entry.is_dir() and now - entry.stat().st_mtime > max_age_sec:
                    shutil.rmtree(entry, ignore_errors=True)
            except OSError:
                continue

    @staticmethod
    def _normalize_emotion(value: str) -> str | dict | None:
        text = str(value or "").strip()
        if not text or text.lower() in {"auto", "off", "none"}:
            return None
        if ":" in text:
            label, _, intensity = text.partition(":")
            label = label.strip()
            try:
                strength = float(intensity)
            except (TypeError, ValueError):
                strength = 0.7
            return {"label": label, "intensity": max(0.0, min(1.0, strength))}
        return text

    async def _materialize_tts_audio(self, client: VoiceStudioClient, result: dict) -> Path:
        """优先用返回的本地 file 路径；跨机部署时回落到 url 流式下载。"""

        raw_file = str(result.get("file") or "").strip()
        if raw_file:
            candidate = Path(raw_file).expanduser()
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                pass
        raw_url = str(result.get("url") or "").strip()
        if not raw_url:
            raise VoiceStudioError("语音工坊返回里既没有可读的 file，也没有 url")
        suffix = "." + (str(result.get("format") or "wav").strip().lstrip(".") or "wav")
        dest = self._new_tmp(suffix)
        await client.download_voice_audio(client.voice_url(raw_url), dest)
        return dest

    async def _finalize_cover(self, mixed: Path) -> Path:
        """可选 ffmpeg loudnorm 统一响度；没有 ffmpeg 就用原始混音文件。"""

        if not _which("ffmpeg"):
            return mixed
        out = mixed.with_name(f"{mixed.stem}_norm.wav")
        ok = await _run_ffmpeg(
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

    async def _first_model(self, client: VoiceStudioClient) -> str:
        data = await client.models()
        models = data.get("models")
        if not isinstance(models, list) or not models:
            raise VoiceStudioError(
                "rvc-worker 上没有可用的 RVC 模型（GET /v1/models 返回空），"
                "请把 .pth 放进 RVC 的 assets/weights 后重试。"
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

    # -- /朗读 --------------------------------------------------------------
    @filter.command("朗读", alias={"语音说"})
    async def tts_cmd(self, event: AstrMessageEvent):
        """用语音工坊合成语音并发送语音条。用法：/朗读 <文本>"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        text = _command_arg(_message_text(event))
        if not text:
            yield event.plain_result(
                "用法：/朗读 <文本>\n例如：/朗读 今天天气不错，适合出去走走\n"
                "也可以直接把「朗读」换成「语音说」。"
            )
            return

        max_len = max(1, _conf_int(self.config, "maxTextLen", 1000))
        notice = ""
        if len(text) > max_len:
            text = text[:max_len]
            notice = f"（文本超过 {max_len} 字，已截断）"

        provider = _conf_str(self.config, "defaultProvider", "") or None
        voice = _conf_str(self.config, "defaultVoice", "") or None
        emotion = self._normalize_emotion(_conf_str(self.config, "defaultEmotion", "auto"))

        try:
            client = await self._get_vs_client()
            result = await client.tts(text, provider=provider, voice=voice, emotion=emotion)
            audio = await self._materialize_tts_audio(client, result)
            work_dir = self._new_tmp_dir()
            audio = await _ensure_wav(audio, work_dir)
            if notice:
                yield event.plain_result(notice)
            yield event.chain_result([_record_for(audio)])
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 朗读失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，不让插件崩
            logger.exception("[%s] /朗读 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 朗读失败：{exc.__class__.__name__}: {exc}")

    # -- /翻唱 --------------------------------------------------------------
    @filter.command("翻唱")
    async def cover_cmd(self, event: AstrMessageEvent):
        """上传歌曲到 rvc-worker，分离 + 换声 + 本地混音后发送语音条。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        arg = _command_arg(_message_text(event))
        if not arg:
            yield event.plain_result(
                "用法：/翻唱 <歌曲绝对路径> [模型]\n"
                '例如：/翻唱 "C:\\music\\song.mp3"\n'
                "提示：路径有空格时请用英文引号包住；模型留空则用插件配置 workerModel，"
                "再留空则用 worker 上的第一个模型。"
            )
            return

        song_raw, model = _split_cover_arg(arg)
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
        if song_path.suffix.lower() not in AUDIO_SUFFIXES:
            yield event.plain_result(
                f"❌ 不支持的音频格式：{song_path.suffix}，支持 "
                + " / ".join(sorted(AUDIO_SUFFIXES))
            )
            return

        progress = _conf_bool(self.config, "sendProgress", True)
        max_duration = max(10, _conf_int(self.config, "maxDurationSec", 600))
        f0_method = _conf_str(self.config, "workerF0Method", "rmvpe") or "rmvpe"
        key_shift = _conf_int(self.config, "workerKeyShift", 0)
        if not model:
            model = _conf_str(self.config, "workerModel", "")

        try:
            client = await self._get_vs_client()
            await asyncio.to_thread(self._cleanup_tmp)

            duration = await _probe_duration(song_path)
            if duration is not None and duration > max_duration:
                raise VoiceStudioError(
                    f"歌曲时长 {_format_seconds(duration)} 超过上限 {max_duration} 秒"
                    "（可在插件配置 maxDurationSec 调整）。"
                )

            if progress:
                yield event.plain_result(
                    f"🎵 开始翻唱：{song_path.name}（{_format_seconds(duration)}）\n"
                    "流程：上传 → 人声分离 → 换声 → 混音，可能需要几分钟…"
                )

            if not model:
                model = await self._first_model(client)

            file_id = await client.upload_file(song_path)
            if progress:
                yield event.plain_result("📤 上传完成，正在做人声分离…")
            separate_job = await client.create_job(
                "separate",
                file_id,
                {"mode": "vocals", "format": "wav"},
            )
            separate = await client.wait_job(separate_job)
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

            work_dir = self._new_tmp_dir()
            vocal_wav = work_dir / "vocal.wav"
            inst_wav = work_dir / "instrumental.wav"
            await client.download_job_output(separate_job, str(vocal_out.get("name") or "vocal.wav"), vocal_wav)
            await client.download_job_output(separate_job, str(inst_out.get("name") or "instrumental.wav"), inst_wav)

            actual_duration = _wav_duration_sec(inst_wav)
            if actual_duration is not None and actual_duration > max_duration:
                raise VoiceStudioError(
                    f"歌曲实际时长 {_format_seconds(actual_duration)} 超过上限 {max_duration} 秒。"
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
            index = _conf_str(self.config, "workerIndex", "")
            if index:
                convert_params["index"] = index
            convert_job = await client.create_job(
                "convert",
                str(vocal_out.get("fileId")),
                convert_params,
            )
            converted = await client.wait_job(convert_job)
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
            await client.download_job_output(
                convert_job,
                str(converted_out.get("name") or "converted.wav"),
                converted_wav,
            )

            if progress:
                yield event.plain_result("🎚️ 换声完成，正在混音…")
            mixed = work_dir / "cover.wav"
            await asyncio.to_thread(
                _mix_cover,
                inst_wav,
                converted_wav,
                vocal_wav,
                mixed,
            )
            final = await self._finalize_cover(mixed)
            yield event.chain_result([_record_for(final)])
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 翻唱失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 兜底，不让插件崩
            logger.exception("[%s] /翻唱 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 翻唱失败：{exc.__class__.__name__}: {exc}")

    # -- /音色 --------------------------------------------------------------
    @filter.command("音色")
    async def voices_cmd(self, event: AstrMessageEvent):
        """列出语音工坊参考音色库。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        try:
            client = await self._get_vs_client()
            voices = await client.list_voices()
        except VoiceStudioError as exc:
            yield event.plain_result(f"❌ 读取音色库失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] /音色 未预期错误", PLUGIN_NAME)
            yield event.plain_result(f"❌ 读取音色库失败：{exc.__class__.__name__}: {exc}")
            return

        if not voices:
            yield event.plain_result(
                "🎧 语音工坊的参考音色库是空的。\n"
                "请先在语音工坊设置页上传/导入参考音频（本地提供商 indextts / gptsovits 必须有参考音色）。"
            )
            return

        default_voice = _conf_str(self.config, "defaultVoice", "")
        lines = [f"🎧 参考音色 {len(voices)} 个" + (f"（当前默认：{default_voice}）" if default_voice else "")]
        limit = 40
        for index, voice in enumerate(voices[:limit], start=1):
            name = str(voice.get("name") or "未命名")
            voice_id = str(voice.get("id") or "?")
            duration = voice.get("durationSec")
            duration_text = ""
            if isinstance(duration, (int, float)):
                duration_text = f" · {_format_seconds(duration)}"
            missing = "" if voice.get("available", True) else " · [文件缺失]"
            lines.append(f"{index}. {name}（{voice_id}）{duration_text}{missing}")
        if len(voices) > limit:
            lines.append(f"…还有 {len(voices) - limit} 个未列出")
        lines.append("提示：在插件配置 defaultVoice 填音色 id 或名称即可指定默认音色。")
        yield event.plain_result("\n".join(lines))

    # -- /语音状态 ----------------------------------------------------------
    @filter.command("语音状态")
    async def status_cmd(self, event: AstrMessageEvent):
        """检查语音工坊与 rvc-worker 健康状态。"""

        denied = self._check_allowed(event)
        if denied:
            yield event.plain_result(denied)
            if hasattr(event, "stop_event"):
                event.stop_event()
            return

        client = await self._get_vs_client()
        lines = ["🩺 语音工坊状态"]

        try:
            state = await client.state()
            settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
            tts_settings = settings.get("tts") if isinstance(settings.get("tts"), dict) else {}
            provider = str(tts_settings.get("provider") or "?")
            voices = state.get("voices")
            voice_count = len(voices) if isinstance(voices, list) else 0
            lines.append(f"✅ 语音工坊：可用（TTS 提供商 {provider}，参考音色 {voice_count} 个）")
        except VoiceStudioError as exc:
            lines.append(f"❌ 语音工坊：不可用（{exc}）")

        try:
            health = await client.worker_health()
            rvc = health.get("rvc") if isinstance(health.get("rvc"), dict) else {}
            gpu = health.get("gpu") if isinstance(health.get("gpu"), dict) else {}
            jobs = health.get("jobs") if isinstance(health.get("jobs"), dict) else {}
            version = str(health.get("version") or "?")
            rvc_state = "可用" if rvc.get("available") else "不可用"
            models = rvc.get("models")
            model_count = len(models) if isinstance(models, list) else "?"
            gpu_name = str(gpu.get("name") or "未知")
            running = jobs.get("running", 0)
            lines.append(
                f"✅ rvc-worker：可用（v{version}，RVC {rvc_state}，模型 {model_count} 个，"
                f"GPU {gpu_name}，进行中任务 {running}）"
            )
        except VoiceStudioError as exc:
            lines.append(f"❌ rvc-worker：不可用（{exc}）")

        lines.append(
            "配置：voiceStudioUrl="
            + (_conf_str(self.config, "voiceStudioUrl", "") or "<未配置>")
            + " / workerUrl="
            + (_conf_str(self.config, "workerUrl", "") or "<未配置>")
        )
        yield event.plain_result("\n".join(lines))


# AstrBot 会扫描插件 main.py 中的 Star 子类；显式导出方便测试与阅读。
__all__ = [
    "VoiceStudioPlugin",
    "VoiceStudioClient",
    "VoiceStudioError",
    "PLUGIN_NAME",
    "PLUGIN_VERSION",
]
