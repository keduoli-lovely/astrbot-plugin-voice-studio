"""本机参考音色库（完全独立，不依赖 DSH）。

目录结构（默认都在插件目录下，可被配置 ``voiceDir`` 覆盖）::

    data/voices/              # 参考音频文件，文件名 = <id><后缀>
    data/voices.json          # 元数据：{version, voices: [{id,name,file,...}]}

设计上参考音频一律 **复制** 进库（而不是原地引用），这样
``/音色删除`` 不会动到用户自己的文件；导入时检查重名并自动加序号。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from .common import (
    MAX_VOICE_BYTES,
    REF_AUDIO_SUFFIXES,
    VoiceStudioError,
    format_bytes,
)


class VoiceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        #: 元数据放在 voices 目录的旁边：data/voices/ -> data/voices.json
        self.meta_path = self.root.parent / f"{self.root.name}.json"
        self._lock = threading.Lock()

    # -- 基础 ---------------------------------------------------------------
    def ensure(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.root.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise VoiceStudioError(f"无法创建音色目录 {self.root}：{exc}") from exc
        if not self.meta_path.exists():
            self._write({"version": 1, "voices": []})

    def _read(self) -> dict:
        self.ensure()
        try:
            with self.meta_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise VoiceStudioError(
                f"音色库元数据损坏：{self.meta_path}（{exc}）。"
                "可以删除该文件后重新 /音色导入。"
            ) from exc
        if not isinstance(data, dict):
            raise VoiceStudioError(f"音色库元数据格式错误：{self.meta_path}")
        voices = data.get("voices")
        if not isinstance(voices, list):
            data["voices"] = []
        return data

    def _write(self, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_name(f"{self.meta_path.name}.{uuid.uuid4().hex[:6]}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.meta_path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise VoiceStudioError(f"写入音色库元数据失败：{exc}") from exc

    # -- 查询 ---------------------------------------------------------------
    def list_voices(self) -> list[dict]:
        with self._lock:
            data = self._read()
        result = []
        for entry in data["voices"]:
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            item = dict(entry)
            file_path = self._entry_path(item)
            item["available"] = file_path is not None and file_path.is_file()
            item["path"] = str(file_path) if file_path is not None else ""
            result.append(item)
        return result

    def resolve(self, key: str | None) -> dict | None:
        """按 id / 名称 / 文件名（不含后缀）查找，大小写不敏感。"""

        wanted = str(key or "").strip()
        if not wanted:
            return None
        voices = self.list_voices()
        for voice in voices:
            if str(voice.get("id")) == wanted:
                return voice
        lowered = wanted.lower()
        for voice in voices:
            if str(voice.get("name") or "").lower() == lowered:
                return voice
        for voice in voices:
            if Path(str(voice.get("file") or "")).stem.lower() == lowered:
                return voice
        return None

    def require(self, key: str | None, purpose: str = "合成") -> dict:
        voice = self.resolve(key)
        if voice is None:
            if not str(key or "").strip():
                raise VoiceStudioError(
                    f"本地提供商{purpose}需要参考音色：请先 /音色导入 <本机音频路径>，"
                    "再在配置 defaultVoice 或 /朗读 -v <音色> 指定。"
                )
            raise VoiceStudioError(
                f"音色库里找不到「{key}」。用 /音色 查看已有音色，或 /音色导入 添加。"
            )
        if not voice.get("available"):
            raise VoiceStudioError(
                f"音色「{voice.get('name')}」的音频文件缺失（{voice.get('file')}），请重新导入。"
            )
        return voice

    def _entry_path(self, entry: dict) -> Path | None:
        filename = str(entry.get("file") or "").strip()
        if not filename or "/" in filename or "\\" in filename or filename in {".", ".."}:
            return None
        return self.root / filename

    def stats(self) -> dict:
        voices = self.list_voices()
        available = [voice for voice in voices if voice.get("available")]
        total = 0
        for voice in available:
            try:
                total += int(voice.get("size") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "dir": str(self.root),
            "meta": str(self.meta_path),
            "count": len(voices),
            "available": len(available),
            "bytes": total,
            "voices": voices,
        }

    # -- 写入 ---------------------------------------------------------------
    def _unique_name(self, name: str, voices: list[dict]) -> str:
        existing = {str(voice.get("name") or "").strip().lower() for voice in voices}
        base = name.strip()
        if base.lower() not in existing:
            return base
        index = 2
        while f"{base} ({index})".lower() in existing:
            index += 1
        return f"{base} ({index})"

    def add(self, source: str | Path, name: str | None = None, description: str = "") -> dict:
        source_path = Path(str(source)).expanduser()
        try:
            is_file = source_path.is_file()
        except OSError:
            is_file = False
        if not is_file:
            raise VoiceStudioError(f"找不到要导入的音频文件：{source_path}")
        suffix = source_path.suffix.lower()
        if suffix not in REF_AUDIO_SUFFIXES:
            raise VoiceStudioError(
                f"不支持的音频格式「{suffix or '(无后缀)'}」，支持："
                + " / ".join(sorted(REF_AUDIO_SUFFIXES))
            )
        try:
            size = source_path.stat().st_size
        except OSError as exc:
            raise VoiceStudioError(f"读取音频文件失败：{exc}") from exc
        if size <= 0:
            raise VoiceStudioError("音频文件是空的，无法导入。")
        if size > MAX_VOICE_BYTES:
            raise VoiceStudioError(
                f"音频文件 {format_bytes(size)} 超过上限 {format_bytes(MAX_VOICE_BYTES)}，"
                "请裁剪后再导入。"
            )

        with self._lock:
            data = self._read()
            voices = [voice for voice in data["voices"] if isinstance(voice, dict)]
            display_name = self._unique_name(
                str(name or source_path.stem).strip() or source_path.stem,
                voices,
            )
            voice_id = "v" + uuid.uuid4().hex[:8]
            filename = f"{voice_id}{suffix}"
            target = self.root / filename
            try:
                self.root.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
            except OSError as exc:
                raise VoiceStudioError(f"复制参考音频到音色库失败：{exc}") from exc
            entry = {
                "id": voice_id,
                "name": display_name,
                "file": filename,
                "description": str(description or "").strip(),
                "size": int(size),
                "createdAt": int(time.time() * 1000),
            }
            voices.append(entry)
            data["voices"] = voices
            try:
                self._write(data)
            except VoiceStudioError:
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
        return entry

    def delete(self, key: str) -> dict:
        """删除一个音色（文件 + 元数据）。"""

        wanted = str(key or "").strip()
        if not wanted:
            raise VoiceStudioError("请指定要删除的音色名称或 id。")
        with self._lock:
            data = self._read()
            voices = [voice for voice in data["voices"] if isinstance(voice, dict)]
            target = None
            for voice in voices:
                if str(voice.get("id")) == wanted:
                    target = voice
                    break
            if target is None:
                lowered = wanted.lower()
                for voice in voices:
                    if str(voice.get("name") or "").lower() == lowered:
                        target = voice
                        break
            if target is None:
                raise VoiceStudioError(f"音色库里找不到「{wanted}」。用 /音色 查看已有音色。")
            file_path = self._entry_path(target)
            data["voices"] = [voice for voice in voices if voice is not target]
            self._write(data)
            if file_path is not None and file_path.is_file():
                try:
                    file_path.unlink()
                except OSError:
                    pass
        return target


__all__ = ["VoiceStore"]
