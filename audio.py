"""音频工具：WAV 编解码 / 混音 + 可选的 ffmpeg 辅助。

混音逻辑移植自 DSH 语音工坊 ``lib/wav.js`` 的 ``mixCover``：
重采样 → 用换声前的人声做 RMS 响度对齐 → 叠加到立体声伴奏 → 峰值归一化到 -1 dBFS。
这里用 numpy 重写；ffmpeg / ffprobe 属于可选能力，找不到就降级，不报错。
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import uuid
from pathlib import Path

from .common import VoiceStudioError


def which(name: str) -> str | None:
    try:
        return shutil.which(name)
    except Exception:  # noqa: BLE001
        return None


def load_numpy():
    try:
        import numpy as np  # noqa: PLC0415 - 可选依赖，延迟导入
    except ImportError as exc:  # pragma: no cover - 依赖问题
        raise VoiceStudioError(
            "翻唱混音需要 numpy：请安装本插件的 requirements.txt（pip install numpy）。"
        ) from exc
    return np


async def run_ffmpeg(args: list[str], timeout: float = 1800.0) -> bool:
    binary = which("ffmpeg")
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


async def probe_duration(path: Path) -> float | None:
    """用 ffprobe 探测时长；没有 ffprobe 时返回 None（不做硬限制）。"""

    binary = which("ffprobe")
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


async def ensure_wav(path: Path, work_dir: Path) -> Path:
    """AstrBot 文档标注语音段建议 wav；非 wav 且本机有 ffmpeg 时转成 wav。"""

    if Path(path).suffix.lower() == ".wav":
        return path
    if not which("ffmpeg"):
        return path
    work_dir.mkdir(parents=True, exist_ok=True)
    out = work_dir / f"{Path(path).stem}_{uuid.uuid4().hex[:6]}.wav"
    ok = await run_ffmpeg(["-y", "-i", str(path), "-ar", "44100", "-ac", "2", str(out)])
    if ok and out.is_file() and out.stat().st_size > 0:
        return out
    return path


# ---------------------------------------------------------------------------
# WAV
# ---------------------------------------------------------------------------


def wav_duration_sec(path: Path) -> float | None:
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


def decode_wav(path: Path) -> dict:
    """解码 PCM / IEEE float WAV 成 float64 交错采样。"""

    np = load_numpy()
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


def to_mono(decoded: dict):
    np = load_numpy()
    samples = decoded["samples"]
    channels = int(decoded["channels"])
    if channels == 1:
        return samples
    frames = samples.size // channels
    return samples[: frames * channels].reshape(frames, channels).mean(axis=1)


def resample(decoded: dict, target_rate: int) -> dict:
    np = load_numpy()
    source_rate = int(decoded["sample_rate"])
    channels = int(decoded["channels"])
    if source_rate == target_rate:
        return decoded
    samples = decoded["samples"]
    frames = samples.size // channels
    if frames <= 1:
        return {
            "sample_rate": target_rate,
            "channels": channels,
            "bits": decoded["bits"],
            "samples": samples[:channels].copy(),
        }
    arr = samples[: frames * channels].reshape(frames, channels)
    out_frames = max(1, int(round(frames * target_rate / source_rate)))
    src_index = np.linspace(0.0, frames - 1.0, out_frames)
    base = np.arange(frames, dtype=np.float64)
    channels_out = [np.interp(src_index, base, arr[:, ch]) for ch in range(channels)]
    out = np.stack(channels_out, axis=1).ravel()
    return {"sample_rate": target_rate, "channels": channels, "bits": decoded["bits"], "samples": out}


def write_wav24(path: Path, sample_rate: int, channels: int, samples) -> None:
    np = load_numpy()
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


def mix_cover(
    instrumental: Path,
    vocal: Path,
    vocal_source: Path | None,
    out_path: Path,
    target_peak: float = 0.891,
) -> Path:
    """换声人声（居中单声道）叠到伴奏：RMS 对齐 + 峰值归一化。"""

    np = load_numpy()
    bed = decode_wav(instrumental)
    voice_decoded = decode_wav(vocal)
    sample_rate = int(bed["sample_rate"])
    channels = int(bed["channels"])
    if channels < 1:
        raise VoiceStudioError("伴奏声道数无效")

    voice = to_mono(resample(voice_decoded, sample_rate))
    bed_samples = bed["samples"].reshape(-1, channels)
    frames = min(bed_samples.shape[0], voice.shape[0])
    if frames <= 0:
        raise VoiceStudioError("混音失败：人声或伴奏为空音频")

    voice = np.asarray(voice[:frames], dtype=np.float64).copy()
    if vocal_source is not None:
        try:
            source = to_mono(resample(decode_wav(vocal_source), sample_rate))
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
    write_wav24(out_path, sample_rate, channels, mixed.ravel())
    return out_path


# 便于测试/调用方少 import 一些
__all__ = [
    "decode_wav",
    "ensure_wav",
    "mix_cover",
    "probe_duration",
    "resample",
    "run_ffmpeg",
    "to_mono",
    "wav_duration_sec",
    "which",
    "write_wav24",
]
