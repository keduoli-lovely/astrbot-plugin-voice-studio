"""语音工坊 AstrBot 插件冒烟测试（无 AstrBot、无网络、无真实提供商）。

运行：
    python tests/smoke.py

覆盖：
1. 在伪 AstrBot（Star / filter / 事件 / Record）环境下能加载插件包（相对导入正常）；
2. 6 个中文命令注册（朗读 + 语音说别名、翻唱、音色、音色导入、音色删除、语音状态）；
3. 插件是**独立的**：源码里没有 DSH 路由 / voiceStudioUrl 之类的引用；
4. 参数解析（/朗读 选项、/音色导入、/音色删除 --yes、/翻唱 引号路径）；
5. 情绪：内置词典、显式指定、8 维向量 / MiniMax 枚举 / CosyVoice 前缀 / MiMo 风格；
6. 音色库：导入（复制）、重名自动加序号、列表、按名称/id/文件名解析、删除、后缀/大小校验；
7. 6 个 TTS 提供商的请求 URL / 方法 / 请求体 / 鉴权头（httpx 全 mock）：
   indextts（Gradio 上传+call+SSE+file 下载）、gptsovits（GET /tts）、
   siliconflow、minimax（hex）、mimo（base64）、openai；
8. /朗读 全链路（provider mock）→ Record 发送；
9. /语音状态 → 检查插件自身配置 + worker /v1/health；
10. /翻唱 全链路（fake worker：上传→分离→下载→换声→混音→Record）与混音质量。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import math
import struct
import sys
import tempfile
import types
import unittest
import wave
from pathlib import Path
from unittest import mock

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PKG = "vs_plugin_under_test"


# ---------------------------------------------------------------------------
# 伪造 AstrBot
# ---------------------------------------------------------------------------


class FakeConfig(dict):
    pass


class FakeLogger:
    def __init__(self):
        self.lines: list[tuple[str, str]] = []

    def debug(self, message, *args):
        self.lines.append(("debug", _fmt(message, args)))

    def info(self, message, *args):
        self.lines.append(("info", _fmt(message, args)))

    def warning(self, message, *args):
        self.lines.append(("warning", _fmt(message, args)))

    def error(self, message, *args):
        self.lines.append(("error", _fmt(message, args)))

    def exception(self, message, *args):
        self.lines.append(("exception", _fmt(message, args)))


def _fmt(message, args) -> str:
    try:
        return str(message) % args if args else str(message)
    except TypeError:
        return f"{message} {args}"


class FakeRecord:
    def __init__(self, file: str = "", url: str = "", path: str = "", **_):
        self.file = file
        self.url = url
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=str(path), path=str(path))


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeContext:
    pass


class FakeEvent:
    def __init__(self, message_str: str = "", group_id: str = ""):
        self.message_str = message_str
        self._group_id = str(group_id or "")
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_name(self):
        return "tester"

    def plain_result(self, text):
        return ("plain", str(text))

    def chain_result(self, chain):
        return ("chain", list(chain))

    def stop_event(self):
        self.stopped = True


class FakeFilter:
    def __init__(self):
        self.commands: list[tuple[str, set[str]]] = []

    def command(self, name, alias=None, priority: int = 0):
        def decorator(func):
            self.commands.append((name, set(alias or ())))
            func.__command_name__ = name
            func.__command_alias__ = set(alias or ())
            return func

        return decorator


FILTER = FakeFilter()
LOGGER = FakeLogger()


def install_fake_astrbot():
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = FakeConfig
    api_mod.logger = LOGGER
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = FakeEvent
    event_mod.filter = FILTER
    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Record = FakeRecord
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = FakeContext
    star_mod.Star = FakeStar
    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components_mod,
            "astrbot.api.star": star_mod,
        }
    )


# ---------------------------------------------------------------------------
# 伪造 httpx
# ---------------------------------------------------------------------------


class _FakeTimeout:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, content: bytes = b"", text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.content = content if isinstance(content, bytes) else bytes(content or b"")
        self._text = text if text is not None else self.content.decode("utf-8", "ignore")

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload

    async def aread(self):
        return self.content

    async def aiter_bytes(self, chunk_size: int = 65536):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start:start + chunk_size]


class _FakeStream:
    def __init__(self, response: FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False


class FakeAsyncClient:
    instances: list["FakeAsyncClient"] = []

    def __init__(self, handler=None, **kwargs):
        self.handler = handler
        self.kwargs = kwargs
        self.requests: list[dict] = []
        self.closed = False
        FakeAsyncClient.instances.append(self)

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        if self.handler is None:
            return FakeResponse(200, {})
        return self.handler(self, method, url, kwargs)

    def stream(self, method, url, **kwargs):
        response = self.handler(self, method, url, kwargs) if self.handler else FakeResponse(200, {}, b"")
        return _FakeStream(response)

    async def aclose(self):
        self.closed = True


class FakeHTTPError(Exception):
    pass


def install_fake_httpx():
    httpx_mod = types.ModuleType("httpx")
    httpx_mod.AsyncClient = FakeAsyncClient
    httpx_mod.Timeout = _FakeTimeout
    httpx_mod.HTTPError = FakeHTTPError
    sys.modules["httpx"] = httpx_mod


# ---------------------------------------------------------------------------
# 加载插件包（相对导入需要包上下文）
# ---------------------------------------------------------------------------


def load_plugin_package():
    for name in list(sys.modules):
        if name == PKG or name.startswith(PKG + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = package
    spec.loader.exec_module(package)
    main_spec = importlib.util.spec_from_file_location(f"{PKG}.main", PLUGIN_DIR / "main.py")
    main = importlib.util.module_from_spec(main_spec)
    sys.modules[f"{PKG}.main"] = main
    main_spec.loader.exec_module(main)
    return main


install_fake_astrbot()
install_fake_httpx()
MAIN = load_plugin_package()
EMOTION = sys.modules[f"{PKG}.emotion"]
VOICE_MOD = sys.modules[f"{PKG}.voice_store"]
TTS = MAIN.tts_providers
AUDIO = MAIN.audio
WORKER = MAIN.worker


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def write_wav(path: Path, *, channels=1, sample_rate=22050, seconds=0.2, freq=440, amp=0.2) -> Path:
    frames = int(sample_rate * seconds)
    data = bytearray()
    for index in range(frames):
        value = int(amp * math.sin(2 * math.pi * freq * index / sample_rate) * 32767)
        for _channel in range(channels):
            data += struct.pack("<h", value)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(bytes(data))
    return path


def make_plugin(**overrides):
    config = {
        "ttsProvider": "indextts",
        "indextts": {"baseUrl": "http://127.0.0.1:7860", "lang": "ZH", "emoWeight": 0.8, "durationFactor": 1.0},
        "gptsovits": {"baseUrl": "http://127.0.0.1:9880", "textLang": "zh", "promptText": "", "promptLang": "zh", "speed": 1.0},
        "siliconflow": {
            "baseUrl": "https://api.siliconflow.cn/v1",
            "apiKey": "sk-test",
            "model": "FunAudioLLM/CosyVoice2-0.5B",
            "voice": "speech:auto",
            "format": "wav",
        },
        "minimax": {
            "baseUrl": "https://api.minimax.chat/v1",
            "apiKey": "mm-test",
            "groupId": "g1",
            "model": "speech-02-hd",
            "voice": "female-shaonv",
            "format": "mp3",
        },
        "mimo": {"baseUrl": "https://mimo.example/v1", "apiKey": "mimo-test", "model": "mimo-v2.5-tts", "voice": "v1", "format": "wav", "style": ""},
        "openai": {"baseUrl": "https://api.openai.com/v1", "apiKey": "oa-test", "model": "gpt-4o-mini-tts", "voice": "alloy", "format": "mp3", "emotionInstructions": True},
        "defaultVoice": "",
        "defaultEmotion": "auto",
        "emotionIntensity": 0.6,
        "ttsSpeed": 1.0,
        "voiceDir": str(Path(tempfile.mkdtemp(prefix="vs_voices_"))),
        "workerUrl": "http://127.0.0.1:7870",
        "workerToken": "worker-token",
        "workerModel": "test.pth",
        "workerIndex": "",
        "workerF0Method": "rmvpe",
        "workerKeyShift": 3,
        "allowedGroups": [],
        "maxTextLen": 1000,
        "maxDurationSec": 600,
        "jobTimeoutSec": 60,
        "requestTimeoutSec": 60,
        "sendProgress": True,
    }
    config.update(overrides)
    return MAIN.VoiceStudioPlugin(FakeContext(), config)


async def collect(generator):
    results = []
    async for item in generator:
        results.append(item)
    return results


class SmokeBase(unittest.TestCase):
    def setUp(self):
        FakeAsyncClient.instances.clear()
        self.tmp = Path(tempfile.mkdtemp(prefix="vs_smoke_"))

    def run_async(self, coro):
        return asyncio.run(coro)

    def run_handler(self, plugin, method, message_str, group_id=""):
        event = FakeEvent(message_str, group_id)
        results = self.run_async(collect(getattr(plugin, method)(event)))
        return event, results


# ---------------------------------------------------------------------------
# 1/2/3. 导入、命令注册、独立性
# ---------------------------------------------------------------------------


class TestPluginShape(SmokeBase):
    def test_package_imports_with_relative_imports(self):
        self.assertTrue(hasattr(MAIN, "VoiceStudioPlugin"))
        self.assertTrue(issubclass(MAIN.VoiceStudioPlugin, FakeStar))
        self.assertIs(MAIN.Record, FakeRecord)
        self.assertEqual(MAIN.PLUGIN_VERSION, "0.2.0")

    def test_commands_registered(self):
        registered = {name: alias for name, alias in FILTER.commands}
        for expected in ("朗读", "音色", "音色导入", "音色删除", "语音状态", "翻唱"):
            self.assertIn(expected, registered, f"命令 {expected} 未注册")
        self.assertIn("语音说", registered["朗读"])
        self.assertEqual(len(FILTER.commands), 6)

    def test_plugin_is_independent_from_dsh(self):
        forbidden = ("tts-studio", "voiceStudioUrl", "voiceStudioToken", "/api/tts", "voice-studio")
        offenders = []
        for path in PLUGIN_DIR.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    offenders.append(f"{path.name}: {token}")
        self.assertEqual(offenders, [], f"插件源码不应再引用 DSH：{offenders}")


# ---------------------------------------------------------------------------
# 4. 参数解析
# ---------------------------------------------------------------------------


class TestArgParsing(SmokeBase):
    def test_read_args_with_options(self):
        options, text = MAIN.parse_read_args('-v 小鲸鱼 -e happy -i 0.8 -p siliconflow -s 1.2 今天 真好')
        self.assertEqual(text, "今天 真好")
        self.assertEqual(options["voice"], "小鲸鱼")
        self.assertEqual(options["emotion"], "happy")
        self.assertEqual(options["intensity"], 0.8)
        self.assertEqual(options["provider"], "siliconflow")
        self.assertEqual(options["speed"], 1.2)

    def test_read_args_without_options(self):
        options, text = MAIN.parse_read_args("你好 世界")
        self.assertEqual(options, {})
        self.assertEqual(text, "你好 世界")

    def test_read_args_quoted_voice_and_clamp(self):
        options, text = MAIN.parse_read_args('-v "小 鲸 鱼" -i 9 -s 0.1 你好')
        self.assertEqual(options["voice"], "小 鲸 鱼")
        self.assertEqual(options["intensity"], 1.0)
        self.assertEqual(options["speed"], 0.5)
        self.assertEqual(text, "你好")

    def test_read_args_invalid_intensity(self):
        with self.assertRaises(MAIN.VoiceStudioError):
            MAIN.parse_read_args("-i 很大 你好")

    def test_import_args(self):
        self.assertEqual(MAIN.parse_import_args('"C:\\a b\\ref.wav" 小鲸鱼'), ("C:\\a b\\ref.wav", "小鲸鱼"))
        self.assertEqual(MAIN.parse_import_args("C:\\ref.wav"), ("C:\\ref.wav", ""))
        self.assertEqual(MAIN.parse_import_args(""), ("", ""))

    def test_delete_args(self):
        self.assertEqual(MAIN.parse_delete_args("小鲸鱼 --yes"), ("小鲸鱼", True))
        self.assertEqual(MAIN.parse_delete_args("小 鲸 鱼"), ("小 鲸 鱼", False))

    def test_cover_args(self):
        self.assertEqual(MAIN.split_cover_args('"C:\\a b\\song.mp3" model.pth'), ("C:\\a b\\song.mp3", "model.pth"))
        self.assertEqual(MAIN.split_cover_args("C:\\song.mp3"), ("C:\\song.mp3", ""))

    def test_command_arg(self):
        self.assertEqual(MAIN._command_arg("/朗读 你好"), "你好")
        self.assertEqual(MAIN._command_arg("语音说 晚安"), "晚安")
        self.assertEqual(MAIN._command_arg("/音色"), "")


# ---------------------------------------------------------------------------
# 5. 情绪
# ---------------------------------------------------------------------------


class TestEmotion(SmokeBase):
    def test_classify_lexicon(self):
        self.assertEqual(EMOTION.classify_text("今天真开心，太好了")["label"], "happy")
        self.assertEqual(EMOTION.classify_text("我很难过，想哭")["label"], "sad")
        self.assertEqual(EMOTION.classify_text("i feel sad and cry")["label"], "sad")
        neutral = EMOTION.classify_text("今天 8 点开会")
        self.assertEqual(neutral["label"], "neutral")
        self.assertEqual(neutral["intensity"], 0.0)

    def test_normalize_explicit(self):
        self.assertEqual(EMOTION.normalize_explicit("开心")["label"], "happy")
        self.assertEqual(EMOTION.normalize_explicit("happy")["label"], "happy")
        self.assertEqual(EMOTION.normalize_explicit("neutral")["label"], "neutral")
        self.assertEqual(EMOTION.normalize_explicit({"label": "sad", "intensity": 0.4})["intensity"], 0.4)
        self.assertIsNone(EMOTION.normalize_explicit("不存在的情绪"))

    def test_resolve_priority(self):
        explicit = EMOTION.resolve_emotion("今天很难过", "happy", mode="auto", default_intensity=0.6)
        self.assertEqual(explicit["label"], "happy")
        auto = EMOTION.resolve_emotion("今天很难过", None, mode="auto", default_intensity=0.6)
        self.assertEqual(auto["label"], "sad")
        off = EMOTION.resolve_emotion("今天很难过", None, mode="off", default_intensity=0.6)
        self.assertEqual(off["label"], "neutral")
        fixed = EMOTION.resolve_emotion("今天很难过", None, mode="happy", default_intensity=0.6)
        self.assertEqual(fixed["label"], "happy")
        with self.assertRaises(MAIN.VoiceStudioError):
            EMOTION.resolve_emotion("x", None, mode="???", default_intensity=0.6)

    def test_provider_mappings(self):
        happy = {"label": "happy", "intensity": 0.8, "source": "explicit"}
        vector = EMOTION.to_vector(happy)
        self.assertEqual(len(vector), 8)
        self.assertEqual(vector[0], 0.8)
        self.assertEqual(EMOTION.to_vector({"label": "neutral"}), [0.0] * 8)
        self.assertEqual(EMOTION.to_minimax(happy), "happy")
        self.assertEqual(EMOTION.to_minimax({"label": "fear"}), "fearful")
        self.assertIn("<|endofprompt|>", EMOTION.to_cosy_prefix(happy))
        self.assertEqual(EMOTION.to_cosy_prefix({"label": "neutral"}), "")
        self.assertIn("明显", EMOTION.to_mimo_style(happy))
        self.assertEqual(EMOTION.to_mimo_style({"label": "neutral"}), "")
        self.assertIn("开心", EMOTION.describe_emotion(happy))


# ---------------------------------------------------------------------------
# 6. 音色库
# ---------------------------------------------------------------------------


class TestVoiceStore(SmokeBase):
    def make_store(self):
        return VOICE_MOD.VoiceStore(self.tmp / "voices")

    def test_add_list_resolve(self):
        store = self.make_store()
        source = write_wav(self.tmp / "ref.wav", seconds=0.1)
        entry = store.add(source, "小鲸鱼")
        self.assertEqual(entry["name"], "小鲸鱼")
        self.assertTrue(entry["id"].startswith("v"))
        self.assertTrue((self.tmp / "voices" / entry["file"]).is_file())
        self.assertTrue(store.meta_path.is_file())
        voices = store.list_voices()
        self.assertEqual(len(voices), 1)
        self.assertTrue(voices[0]["available"])
        self.assertEqual(store.resolve("小鲸鱼")["id"], entry["id"])
        self.assertEqual(store.resolve(entry["id"])["name"], "小鲸鱼")
        self.assertEqual(store.resolve(Path(entry["file"]).stem)["id"], entry["id"])
        self.assertIsNone(store.resolve("不存在"))
        self.assertEqual(store.stats()["count"], 1)

    def test_duplicate_name_gets_suffix(self):
        store = self.make_store()
        source = write_wav(self.tmp / "ref.wav", seconds=0.1)
        first = store.add(source, "音色")
        second = store.add(source, "音色")
        self.assertEqual(first["name"], "音色")
        self.assertEqual(second["name"], "音色 (2)")

    def test_reject_bad_extension_and_missing(self):
        store = self.make_store()
        bad = self.tmp / "note.txt"
        bad.write_text("not audio", encoding="utf-8")
        with self.assertRaises(MAIN.VoiceStudioError):
            store.add(bad)
        with self.assertRaises(MAIN.VoiceStudioError):
            store.add(self.tmp / "missing.wav")

    def test_reject_oversize(self):
        store = self.make_store()
        big = self.tmp / "big.wav"
        with big.open("wb") as handle:
            handle.seek(VOICE_MOD.MAX_VOICE_BYTES + 10)
            handle.write(b"x")
        with self.assertRaises(MAIN.VoiceStudioError) as ctx:
            store.add(big)
        self.assertIn("超过上限", str(ctx.exception))

    def test_delete(self):
        store = self.make_store()
        entry = store.add(write_wav(self.tmp / "ref.wav", seconds=0.1), "删除我")
        removed = store.delete("删除我")
        self.assertEqual(removed["id"], entry["id"])
        self.assertEqual(store.list_voices(), [])
        self.assertFalse((self.tmp / "voices" / entry["file"]).exists())
        with self.assertRaises(MAIN.VoiceStudioError):
            store.delete("删除我")


# ---------------------------------------------------------------------------
# 7. 提供商请求（mock httpx）
# ---------------------------------------------------------------------------


class TestProviders(SmokeBase):
    def make_client(self, handler):
        return FakeAsyncClient(handler=handler)

    def test_indextts_gradio_protocol(self):
        ref = self.tmp / "ref.mp3"
        ref.write_bytes(b"reference-audio")
        dest = self.tmp / "out.wav"
        calls: list[dict] = []

        def handler(client, method, url, kwargs):
            if url.endswith("/gradio_api/upload"):
                calls.append({"kind": "upload", "url": url, "files": list((kwargs.get("files") or {}).keys())})
                return FakeResponse(200, ["/tmp/gradio/ref.mp3"])
            if url.endswith("/gradio_api/call/gen_single") and method == "POST":
                calls.append({"kind": "call", "url": url, "json": kwargs.get("json")})
                return FakeResponse(200, {"event_id": "ev-1"})
            if url.endswith("/gradio_api/call/gen_single/ev-1"):
                calls.append({"kind": "sse", "url": url})
                return FakeResponse(200, text='event: complete\ndata: [{"path": "/tmp/gradio/out.wav"}]\n\n')
            if "/gradio_api/file=" in url:
                calls.append({"kind": "file", "url": url})
                return FakeResponse(200, content=b"INDEXWAV")
            raise AssertionError(f"unexpected {method} {url}")

        emotion = {"label": "happy", "intensity": 0.8, "source": "explicit"}
        result = self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "indextts",
                {"baseUrl": "http://127.0.0.1:7860", "lang": "ZH", "emoWeight": 0.8, "durationFactor": 1.0},
                text="你好世界",
                ref_path=ref,
                emotion=emotion,
                speed=1.0,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"INDEXWAV")
        self.assertEqual(result["provider"], "indextts")
        self.assertTrue(result["emotionApplied"])
        kinds = [call["kind"] for call in calls]
        self.assertEqual(kinds, ["upload", "call", "sse", "file"])
        self.assertEqual(calls[0]["files"], ["files"])
        payload = calls[1]["json"]["data"]
        self.assertEqual(payload[0], TTS.INDEX_EMO_VECTOR)
        self.assertEqual(payload[2], "你好世界")
        self.assertEqual(payload[3], "ZH")
        self.assertAlmostEqual(payload[5], 0.8)
        self.assertAlmostEqual(payload[6], 0.8)  # happy 维度
        self.assertEqual(payload[6:14].count(0.0), 7)

    def test_gptsovits_get_tts(self):
        ref = self.tmp / "ref.wav"
        ref.write_bytes(b"ref")
        dest = self.tmp / "out.wav"
        captured: dict = {}

        def handler(client, method, url, kwargs):
            captured.update({"method": method, "url": url})
            return FakeResponse(200, content=b"GPTSOVITS")

        result = self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "gptsovits",
                {"baseUrl": "http://127.0.0.1:9880", "textLang": "zh", "promptText": "", "promptLang": "zh", "speed": 1.0},
                text="你好",
                ref_path=ref,
                emotion={"label": "neutral", "intensity": 0, "source": "default"},
                speed=1.2,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"GPTSOVITS")
        self.assertEqual(captured["method"], "GET")
        self.assertIn("/tts?", captured["url"])
        self.assertIn("text=%E4%BD%A0%E5%A5%BD", captured["url"])
        self.assertIn("speed_factor=1.2", captured["url"])
        self.assertIn("ref_audio_path=", captured["url"])
        self.assertIn("ref_free=true", captured["url"])
        self.assertFalse(result["emotionApplied"])

    def test_siliconflow_speech(self):
        dest = self.tmp / "out.wav"
        captured: dict = {}

        def handler(client, method, url, kwargs):
            captured.update({"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")})
            return FakeResponse(200, content=b"SILICON")

        self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "siliconflow",
                {"baseUrl": "https://api.siliconflow.cn/v1", "apiKey": "sk-1", "model": "m", "voice": "v", "format": "wav"},
                text="你好",
                ref_path=None,
                emotion={"label": "happy", "intensity": 0.7, "source": "explicit"},
                speed=1.0,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"SILICON")
        self.assertEqual(captured["url"], "https://api.siliconflow.cn/v1/audio/speech")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer sk-1")
        self.assertEqual(captured["json"]["voice"], "v")
        self.assertEqual(captured["json"]["model"], "m")
        self.assertTrue(captured["json"]["input"].startswith("Can you say it with a happy emotion? <|endofprompt|> 你好"))

    def test_minimax_hex_decode(self):
        dest = self.tmp / "out.mp3"
        captured: dict = {}

        def handler(client, method, url, kwargs):
            captured.update({"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")})
            return FakeResponse(
                200,
                {"base_resp": {"status_code": 0}, "data": {"audio": b"MINIMAX".hex()}},
            )

        result = self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "minimax",
                {"baseUrl": "https://api.minimax.chat/v1", "apiKey": "mm-1", "groupId": "g1", "model": "m", "voice": "female-shaonv", "format": "mp3"},
                text="你好",
                ref_path=None,
                emotion={"label": "happy", "intensity": 0.7, "source": "explicit"},
                speed=1.0,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"MINIMAX")
        self.assertIn("/t2a_v2?", captured["url"])
        self.assertIn("GroupId=g1", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer mm-1")
        self.assertEqual(captured["json"]["voice_setting"]["emotion"], "happy")
        self.assertEqual(captured["json"]["output_format"], "hex")
        self.assertTrue(result["emotionApplied"])

    def test_minimax_error_status(self):
        dest = self.tmp / "out.mp3"

        def handler(client, method, url, kwargs):
            return FakeResponse(200, {"base_resp": {"status_code": 1004, "status_msg": "invalid key"}})

        with self.assertRaises(MAIN.VoiceStudioError):
            self.run_async(
                TTS.synthesize(
                    self.make_client(handler),
                    "minimax",
                    {"baseUrl": "https://api.minimax.chat/v1", "apiKey": "x", "groupId": "", "format": "mp3"},
                    text="你好",
                    ref_path=None,
                    emotion=None,
                    speed=None,
                    dest=dest,
                    timeout=60,
                )
            )

    def test_mimo_chat_completions(self):
        dest = self.tmp / "out.wav"
        captured: dict = {}

        def handler(client, method, url, kwargs):
            captured.update({"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")})
            audio_b64 = base64.b64encode(b"MIMO").decode()
            return FakeResponse(200, {"choices": [{"message": {"audio": {"data": audio_b64}}}]})

        result = self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "mimo",
                {"baseUrl": "https://mimo.example/v1", "apiKey": "mimo-1", "model": "m", "voice": "v", "format": "wav"},
                text="你好",
                ref_path=None,
                emotion={"label": "happy", "intensity": 0.7, "source": "explicit"},
                speed=1.0,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"MIMO")
        self.assertEqual(captured["url"], "https://mimo.example/v1/chat/completions")
        self.assertEqual(captured["headers"]["api-key"], "mimo-1")
        messages = captured["json"]["messages"]
        self.assertEqual(messages[0]["role"], "user")
        self.assertIn("开心", messages[0]["content"])
        self.assertEqual(messages[-1], {"role": "assistant", "content": "你好"})
        self.assertTrue(result["emotionApplied"])

    def test_openai_speech_with_instructions(self):
        dest = self.tmp / "out.mp3"
        captured: dict = {}

        def handler(client, method, url, kwargs):
            captured.update({"url": url, "headers": kwargs.get("headers"), "json": kwargs.get("json")})
            return FakeResponse(200, content=b"OPENAI")

        self.run_async(
            TTS.synthesize(
                self.make_client(handler),
                "openai",
                {"baseUrl": "https://api.openai.com/v1", "apiKey": "oa-1", "model": "gpt-4o-mini-tts", "voice": "alloy", "format": "mp3"},
                text="你好",
                ref_path=None,
                emotion={"label": "happy", "intensity": 0.7, "source": "explicit"},
                speed=1.0,
                dest=dest,
                timeout=60,
            )
        )
        self.assertEqual(dest.read_bytes(), b"OPENAI")
        self.assertEqual(captured["url"], "https://api.openai.com/v1/audio/speech")
        self.assertEqual(captured["json"]["voice"], "alloy")
        self.assertEqual(captured["json"]["instructions"], "Can you say it with a happy emotion?")

    def test_missing_api_key_fails_with_chinese_error(self):
        for provider in ("siliconflow", "minimax", "mimo", "openai"):
            with self.assertRaises(MAIN.VoiceStudioError) as ctx:
                self.run_async(
                    TTS.synthesize(
                        self.make_client(lambda *a: FakeResponse(200, content=b"x")),
                        provider,
                        {"baseUrl": "http://x", "apiKey": "", "format": "wav"},
                        text="你好",
                        ref_path=None,
                        emotion=None,
                        speed=None,
                        dest=self.tmp / f"{provider}.wav",
                        timeout=30,
                    )
                )
            self.assertIn("apiKey", str(ctx.exception))

    def test_resolve_format(self):
        self.assertEqual(TTS.resolve_format("indextts", {"format": "mp3"}), "wav")
        self.assertEqual(TTS.resolve_format("openai", {}), "mp3")
        self.assertEqual(TTS.resolve_format("openai", {"format": "wav"}), "wav")
        with self.assertRaises(MAIN.VoiceStudioError):
            TTS.resolve_format("openai", {"format": "aiff"})


# ---------------------------------------------------------------------------
# 8. /朗读
# ---------------------------------------------------------------------------


class TestTtsCommand(SmokeBase):
    def _fake_synth(self, calls, out_bytes=b"RIFF....WAVEfake"):
        async def fake(client, provider, cfg, *, text, ref_path, emotion, speed, dest, timeout):
            calls.append(
                {
                    "provider": provider,
                    "text": text,
                    "ref_path": Path(ref_path) if ref_path else None,
                    "emotion": dict(emotion or {}),
                    "speed": speed,
                    "dest": Path(dest),
                    "timeout": timeout,
                }
            )
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(out_bytes)
            return {"provider": provider, "path": Path(dest), "format": Path(dest).suffix.lstrip("."), "emotionApplied": True}

        return fake

    def test_tts_flow_with_auto_emotion_and_voice(self):
        plugin = make_plugin()
        voice = plugin._store().add(write_wav(self.tmp / "ref.wav", seconds=0.1), "小鲸鱼")
        calls: list[dict] = []
        with mock.patch.object(TTS, "synthesize", self._fake_synth(calls)):
            _event, results = self.run_handler(plugin, "tts_cmd", "/朗读 今天真开心，太好了")
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(call["provider"], "indextts")
        self.assertEqual(call["text"], "今天真开心，太好了")
        self.assertEqual(call["emotion"]["label"], "happy")
        expected_ref = plugin._store().resolve(voice["id"])["path"]
        self.assertEqual(call["ref_path"], Path(expected_ref))
        self.assertEqual(results[-1][0], "chain")
        record = results[-1][1][0]
        self.assertTrue(Path(record.path).is_file())

    def test_tts_options_override(self):
        plugin = make_plugin()
        plugin._store().add(write_wav(self.tmp / "ref.wav", seconds=0.1), "小鲸鱼")
        calls: list[dict] = []
        with mock.patch.object(TTS, "synthesize", self._fake_synth(calls)):
            self.run_handler(
                plugin,
                "tts_cmd",
                "/朗读 -v 小鲸鱼 -e sad -i 0.9 -p indextts -s 1.2 我很难过",
            )
        call = calls[0]
        self.assertEqual(call["emotion"]["label"], "sad")
        self.assertEqual(call["emotion"]["intensity"], 0.9)
        self.assertEqual(call["speed"], 1.2)

    def test_online_provider_uses_voice_override(self):
        plugin = make_plugin()
        calls: list[dict] = []
        with mock.patch.object(TTS, "synthesize", self._fake_synth(calls)):
            self.run_handler(plugin, "tts_cmd", "/朗读 -p siliconflow -v speech:auto 你好")
        call = calls[0]
        self.assertEqual(call["provider"], "siliconflow")
        self.assertIsNone(call["ref_path"])
        self.assertEqual(call["dest"].suffix, ".wav")

    def test_empty_voice_library_returns_chinese_error(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "tts_cmd", "/朗读 你好")
        self.assertEqual(len(results), 1)
        self.assertIn("音色库是空的", results[0][1])

    def test_usage_without_text(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "tts_cmd", "/朗读")
        self.assertIn("用法", results[0][1])

    def test_group_whitelist(self):
        plugin = make_plugin(allowedGroups=["111"])
        event, results = self.run_handler(plugin, "tts_cmd", "/朗读 hi", group_id="222")
        self.assertTrue(event.stopped)
        self.assertIn("白名单", results[0][1])
        self.assertEqual(FakeAsyncClient.instances, [])


# ---------------------------------------------------------------------------
# 9. /音色 /音色导入 /音色删除
# ---------------------------------------------------------------------------


class TestVoiceCommands(SmokeBase):
    def test_import_list_delete(self):
        plugin = make_plugin()
        ref = write_wav(self.tmp / "ref.wav", seconds=0.1)
        _event, results = self.run_handler(plugin, "voice_import_cmd", f'/音色导入 "{ref}" 小鲸鱼')
        self.assertIn("已导入", results[0][1])
        self.assertIn("小鲸鱼", results[0][1])

        _event, results = self.run_handler(plugin, "voices_cmd", "/音色")
        self.assertIn("小鲸鱼", results[0][1])
        self.assertIn("参考音色 1 个", results[0][1])

        _event, results = self.run_handler(plugin, "voice_delete_cmd", "/音色删除 小鲸鱼")
        self.assertIn("确认删除", results[0][1])

        _event, results = self.run_handler(plugin, "voice_delete_cmd", "/音色删除 小鲸鱼 --yes")
        self.assertIn("已删除", results[0][1])
        self.assertEqual(plugin._store().list_voices(), [])

    def test_import_missing_file(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "voice_import_cmd", "/音色导入 C:\\nope\\x.wav")
        self.assertIn("找不到要导入的音频文件", results[0][1])

    def test_import_usage(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "voice_import_cmd", "/音色导入")
        self.assertIn("用法", results[0][1])

    def test_delete_unknown(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "voice_delete_cmd", "/音色删除 不存在 --yes")
        self.assertIn("找不到", results[0][1])

    def test_empty_library_message(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "voices_cmd", "/音色")
        self.assertIn("音色库是空的", results[0][1])


# ---------------------------------------------------------------------------
# 10. /语音状态
# ---------------------------------------------------------------------------


class TestStatus(SmokeBase):
    def test_status_reports_provider_library_and_worker(self):
        plugin = make_plugin()
        plugin._store().add(write_wav(self.tmp / "ref.wav", seconds=0.1), "小鲸鱼")
        health_payload = {
            "ok": True,
            "version": "1.0.0",
            "rvc": {"available": True, "models": [{"id": "m1"}]},
            "gpu": {"name": "RTX 4090"},
            "jobs": {"running": 0},
        }

        async def fake_health(client, base, token):
            return health_payload

        with mock.patch.object(WORKER, "health", fake_health):
            _event, results = self.run_handler(plugin, "status_cmd", "/语音状态")
        text = results[0][1]
        self.assertIn("✅ TTS 提供商", text)
        self.assertIn("✅ 音色库", text)
        self.assertIn("1 个", text)
        self.assertIn("✅ rvc-worker", text)
        self.assertIn("RTX 4090", text)
        self.assertIn("不依赖 DSH", text)

    def test_status_without_worker_url(self):
        plugin = make_plugin(workerUrl="")
        with mock.patch.object(WORKER, "health", side_effect=AssertionError("不应被调用")):
            _event, results = self.run_handler(plugin, "status_cmd", "/语音状态")
        self.assertIn("❌ rvc-worker", results[0][1])

    def test_status_online_provider_missing_key(self):
        plugin = make_plugin(ttsProvider="siliconflow", siliconflow={**make_plugin().config["siliconflow"], "apiKey": ""})
        with mock.patch.object(WORKER, "health", side_effect=MAIN.VoiceStudioError("down")):
            _event, results = self.run_handler(plugin, "status_cmd", "/语音状态")
        text = results[0][1]
        self.assertIn("⚠️ TTS 提供商", text)
        self.assertIn("apiKey", text)


# ---------------------------------------------------------------------------
# 11. /翻唱 + 混音
# ---------------------------------------------------------------------------


class TestCover(SmokeBase):
    def test_mix_cover(self):
        inst = write_wav(self.tmp / "inst.wav", channels=2, sample_rate=44100, seconds=0.5, freq=220, amp=0.3)
        vocal = write_wav(self.tmp / "vocal.wav", channels=1, sample_rate=22050, seconds=0.5, freq=440, amp=0.15)
        source = write_wav(self.tmp / "source.wav", channels=1, sample_rate=22050, seconds=0.5, freq=440, amp=0.3)
        out = self.tmp / "mixed.wav"
        AUDIO.mix_cover(inst, vocal, source, out)
        self.assertTrue(out.is_file())
        self.assertAlmostEqual(AUDIO.wav_duration_sec(out), 0.5, places=2)
        decoded = AUDIO.decode_wav(out)
        self.assertEqual(decoded["channels"], 2)
        self.assertEqual(decoded["sample_rate"], 44100)
        peak = float(abs(decoded["samples"]).max())
        self.assertAlmostEqual(peak, 0.891, places=2)

    def _run_cover(self, plugin, song: Path):
        vocal = write_wav(self.tmp / "sep_vocal.wav", channels=1, sample_rate=44100, seconds=0.4, freq=300, amp=0.15)
        inst = write_wav(self.tmp / "sep_inst.wav", channels=2, sample_rate=44100, seconds=0.4, freq=200, amp=0.2)
        converted = write_wav(self.tmp / "sep_conv.wav", channels=1, sample_rate=44100, seconds=0.4, freq=310, amp=0.2)
        jobs: list[tuple] = []

        async def fake_upload(client, base, token, path):
            return "file-song"

        async def fake_create(client, base, token, job_type, file_id, params=None):
            jobs.append((job_type, file_id, dict(params or {})))
            return f"job-{job_type}"

        async def fake_wait(client, base, token, job_id, *, timeout_s=0, poll_interval=0, logger=None):
            if job_id == "job-separate":
                return {"status": "succeeded", "outputs": [
                    {"name": "vocal.wav", "fileId": "file-vocal"},
                    {"name": "instrumental.wav", "fileId": "file-inst"},
                ]}
            return {"status": "succeeded", "outputs": [{"name": "converted.wav", "fileId": "file-conv"}]}

        async def fake_download(client, base, token, job_id, name, dest):
            source = converted if "convert" in job_id else (vocal if "vocal" in name else inst)
            dest = Path(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(source.read_bytes())
            return dest

        with mock.patch.object(WORKER, "upload_file", fake_upload), \
                mock.patch.object(WORKER, "create_job", fake_create), \
                mock.patch.object(WORKER, "wait_job", fake_wait), \
                mock.patch.object(WORKER, "download_job_output", fake_download):
            _event, results = self.run_handler(plugin, "cover_cmd", f'/翻唱 "{song}"')
        return jobs, results

    def test_cover_end_to_end(self):
        plugin = make_plugin()
        song = write_wav(self.tmp / "song.wav", channels=2, sample_rate=44100, seconds=0.4, freq=200, amp=0.2)
        jobs, results = self._run_cover(plugin, song)
        self.assertEqual([job[0] for job in jobs], ["separate", "convert"])
        convert_params = jobs[1][2]
        self.assertEqual(convert_params["model"], "test.pth")
        self.assertEqual(convert_params["f0_method"], "rmvpe")
        self.assertEqual(convert_params["f0_up_key"], 3)
        self.assertEqual(convert_params["index_rate"], 0.75)
        final_kind, final_chain = results[-1]
        self.assertEqual(final_kind, "chain")
        record = final_chain[0]
        self.assertTrue(Path(record.path).is_file())
        mixed = AUDIO.decode_wav(Path(record.path))
        self.assertEqual(mixed["channels"], 2)
        self.assertGreater(float(abs(mixed["samples"]).max()), 0.5)

    def test_cover_missing_file(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "cover_cmd", f"/翻唱 {self.tmp / 'nope.mp3'}")
        self.assertIn("找不到歌曲文件", results[0][1])

    def test_cover_usage(self):
        plugin = make_plugin()
        _event, results = self.run_handler(plugin, "cover_cmd", "/翻唱")
        self.assertIn("用法", results[0][1])


# ---------------------------------------------------------------------------
# 12. httpx 客户端构造
# ---------------------------------------------------------------------------


class TestHttpClient(SmokeBase):
    def test_client_construction(self):
        plugin = make_plugin()

        async def scenario():
            client = await plugin._http()
            await plugin.terminate()
            return client

        client = self.run_async(scenario())
        self.assertIsInstance(client, FakeAsyncClient)
        self.assertTrue(client.kwargs["follow_redirects"])
        self.assertIsInstance(client.kwargs["timeout"], _FakeTimeout)
        self.assertTrue(client.closed)


def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
