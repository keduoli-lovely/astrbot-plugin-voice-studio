"""语音工坊 AstrBot 插件冒烟测试（无 AstrBot、无网络、无真实提供商）。

运行：
    python tests/smoke.py

覆盖（共 71 个用例）：
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
10. /翻唱 全链路（fake worker：上传→分离→下载→换声→混音→Record）与混音质量；
11. data/settings.json：补丁合并、settings > AstrBot 配置的优先级、非法值拒绝、损坏文件报错；
12. 设置页 Web API：路由注册、GET/POST settings、音色上传/重名/后缀/超限/路径穿越、
    音色列表/导入/删除/试听音频、试听合成（base64）、worker 健康检查与进程启停；
13. 设置页静态资源：index.html 引用的 app.js/style.css 都存在、JS 只走 bridge、
    i18n 的 views.studio.title/description 存在；
14. _conf_schema.json：由设置页接管的字段标记 invisible，可见项保留，且不含 RVC WebUI 字样。
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
    def __init__(self):
        self.web_routes: list[dict] = []

    def register_web_api(self, route, handler, methods, desc=""):
        self.web_routes.append(
            {"route": route, "handler": handler, "methods": list(methods), "desc": desc}
        )


class FakePlain:
    def __init__(self, text):
        self.text = str(text)


class FakeEvent:
    def __init__(self, message_str: str = "", group_id: str = "", result=None):
        self.message_str = message_str
        self._group_id = str(group_id or "")
        self._result = result
        self.stopped = False
        self.sent = []

    def get_result(self):
        return self._result

    async def send(self, result):
        self.sent.append(result)
        return result

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


class FakeEventMessageType:
    ALL = "ALL"
    PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
    GROUP_MESSAGE = "GROUP_MESSAGE"


class FakeFilter:
    EventMessageType = FakeEventMessageType

    def __init__(self):
        self.commands: list[tuple[str, set[str]]] = []
        self.hooks: list = []
        self.listeners: list = []

    def on_decorating_result(self):
        def decorator(func):
            self.hooks.append(func)
            return func

        return decorator

    def on_llm_response(self):
        def decorator(func):
            self.hooks.append(func)
            return func

        return decorator

    def on_llm_request(self):
        def decorator(func):
            self.hooks.append(func)
            return func

        return decorator

    def event_message_type(self, message_type):
        def decorator(func):
            self.listeners.append(func)
            return func

        return decorator

    def command(self, name, alias=None, priority: int = 0):
        def decorator(func):
            self.commands.append((name, set(alias or ())))
            func.__command_name__ = name
            func.__command_alias__ = set(alias or ())
            return func

        return decorator


FILTER = FakeFilter()
LOGGER = FakeLogger()


# ---------------------------------------------------------------------------
# 伪造 astrbot.api.web（插件设置页后端 API）
# ---------------------------------------------------------------------------


class FakeWebResponse:
    def __init__(self, data=None, status_code: int = 200, error: str | None = None, path=None, filename=None, content_type=None):
        self.data = data
        self.status_code = status_code
        self.error = error
        self.path = path
        self.filename = filename
        self.content_type = content_type

    @property
    def message(self):
        if self.error:
            return self.error
        if isinstance(self.data, dict):
            return self.data.get("message")
        return None


class FakeWebUploadFile:
    def __init__(self, filename: str, content: bytes):
        self.filename = filename
        self.content = bytes(content)
        self.content_length = len(self.content)
        self.content_type = "application/octet-stream"

    async def read(self, size: int = -1):
        if size is None or size < 0:
            return self.content
        return self.content[:size]


class FakeWebRequest:
    def __init__(self, method="GET", json_data=None, form_data=None, files_data=None, query=None):
        self.method = method
        self._json = json_data
        self._form = form_data or {}
        self._files = files_data or {}
        self.query = query or {}

    async def json(self, default=None):
        return self._json if self._json is not None else default

    async def form(self):
        return self._form

    async def files(self):
        return self._files


def fake_json_response(data=None, *, status_code: int = 200, headers=None):
    return FakeWebResponse(data=data, status_code=status_code)


def fake_error_response(message, *, status_code: int = 400, data=None, headers=None):
    return FakeWebResponse(
        data={"status": "error", "message": message},
        status_code=status_code,
        error=str(message),
    )


def fake_file_response(path, *, filename=None, content_type=None, headers=None):
    return FakeWebResponse(path=str(path), filename=filename, content_type=content_type)


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
    web_mod = types.ModuleType("astrbot.api.web")
    web_mod.PluginUploadFile = FakeWebUploadFile
    web_mod.json_response = fake_json_response
    web_mod.error_response = fake_error_response
    web_mod.file_response = fake_file_response
    web_mod.request = None
    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components_mod,
            "astrbot.api.star": star_mod,
            "astrbot.api.web": web_mod,
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
SETTINGS_MOD = sys.modules[f"{PKG}.settings_store"]
WEB = sys.modules[f"{PKG}.web_api"]
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
    context = FakeContext()
    plugin = MAIN.VoiceStudioPlugin(context, config)
    # 把运行数据/设置文件隔离到临时目录，避免污染仓库里的 data/
    plugin._data_dir = Path(tempfile.mkdtemp(prefix="vs_data_"))
    plugin._tmp_dir().mkdir(parents=True, exist_ok=True)
    plugin._settings_store = MAIN.SettingsStore(plugin._data_dir / "settings.json")
    return plugin


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
        self.assertEqual(MAIN.PLUGIN_VERSION, "0.4.4")

    def test_commands_registered(self):
        registered = {name: alias for name, alias in FILTER.commands}
        for expected in ("朗读", "音色", "音色导入", "音色删除", "语音状态", "翻唱"):
            self.assertIn(expected, registered, f"命令 {expected} 未注册")
        self.assertIn("语音说", registered["朗读"])
        self.assertIn("唱歌", registered["翻唱"])
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


# ---------------------------------------------------------------------------
# 13. data/settings.json（设置页存储）与合并优先级
# ---------------------------------------------------------------------------


class TestSettingsStore(SmokeBase):
    def test_patch_and_merge_precedence(self):
        store = SETTINGS_MOD.SettingsStore(self.tmp / "settings.json")
        config = {
            "ttsProvider": "indextts",
            "siliconflow": {"baseUrl": "https://sf.example/v1", "apiKey": ""},
            "workerUrl": "http://old:7870",
            "workerF0Method": "pm",
            "defaultVoice": "cfg-voice",
        }
        store.patch(
            {
                "ttsProvider": "siliconflow",
                "providers": {"siliconflow": {"apiKey": "sk-live"}},
                "worker": {"url": "http://new:7870"},
                "defaultVoice": "stored-voice",
            }
        )
        merged = SETTINGS_MOD.merge_settings(store.read(), config)
        self.assertEqual(merged["ttsProvider"], "siliconflow")
        self.assertEqual(merged["providers"]["siliconflow"]["apiKey"], "sk-live")
        # 未在 settings.json 里写的字段仍从 AstrBot 配置回退
        self.assertEqual(merged["providers"]["siliconflow"]["baseUrl"], "https://sf.example/v1")
        self.assertEqual(merged["worker"]["url"], "http://new:7870")
        self.assertEqual(merged["worker"]["f0Method"], "pm")
        self.assertEqual(merged["defaultVoice"], "stored-voice")

    def test_nested_patch_keeps_other_provider_keys(self):
        store = SETTINGS_MOD.SettingsStore(self.tmp / "settings.json")
        store.patch({"providers": {"openai": {"apiKey": "a"}}})
        store.patch({"providers": {"openai": {"voice": "nova"}}})
        cfg = store.read()["providers"]["openai"]
        self.assertEqual(cfg["apiKey"], "a")
        self.assertEqual(cfg["voice"], "nova")

    def test_validate_rejects_bad_values(self):
        validate = SETTINGS_MOD.validate_settings_patch
        with self.assertRaises(MAIN.VoiceStudioError):
            validate({"ttsProvider": "nope"})
        with self.assertRaises(MAIN.VoiceStudioError):
            validate({"providers": {"openai": {"format": "aiff"}}})
        with self.assertRaises(MAIN.VoiceStudioError):
            validate({"providers": {"nope": {}}})
        cleaned = validate({"worker": {"keyShift": 99, "f0Method": "pm"}})
        self.assertEqual(cleaned["worker"]["keyShift"], 24)
        self.assertEqual(cleaned["worker"]["f0Method"], "pm")
        with self.assertRaises(MAIN.VoiceStudioError):
            validate({"worker": {"f0Method": "nope"}})

    def test_corrupt_file_raises(self):
        path = self.tmp / "settings.json"
        path.write_text("{not-json", encoding="utf-8")
        store = SETTINGS_MOD.SettingsStore(path)
        with self.assertRaises(MAIN.VoiceStudioError):
            store.read()

    def test_partial_default_voice_patch(self):
        store = SETTINGS_MOD.SettingsStore(self.tmp / "settings.json")
        store.patch({"defaultVoice": "v123"})
        self.assertEqual(store.read()["defaultVoice"], "v123")


# ---------------------------------------------------------------------------
# 14. 设置页后端 Web API
# ---------------------------------------------------------------------------


class TestWebAPI(SmokeBase):
    def use_request(self, **kwargs):
        WEB.request = FakeWebRequest(**kwargs)

    def call(self, handler):
        return self.run_async(handler())

    def test_routes_registered(self):
        plugin = make_plugin()
        self.assertIsNotNone(plugin._web_api)
        routes = set(plugin._web_api.routes)
        for expected in (
            "settings",
            "voices",
            "voices/upload",
            "voices/import",
            "voices/delete",
            "voices/audio",
            "test",
            "worker/health",
            "worker/process",
        ):
            self.assertIn(expected, routes)
        context_routes = [item["route"] for item in plugin.context.web_routes]
        self.assertTrue(all("/astrbot" in route or "/" in route for route in context_routes))
        methods = {item["route"].rsplit("/", 1)[-1]: item["methods"] for item in plugin.context.web_routes}
        self.assertIn("get", [m.lower() for m in methods.get("settings", [])])
        self.assertIn("post", [m.lower() for m in methods.get("settings", [])])

    def test_get_settings(self):
        plugin = make_plugin()
        self.use_request(method="GET")
        response = self.call(plugin._web_api.handle_settings)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["settings"]["ttsProvider"], "indextts")
        self.assertEqual(len(response.data["providers"]), 6)
        self.assertEqual(response.data["providers"]["indextts"]["kind"], "local")

    def test_post_settings_persists_to_settings_json(self):
        plugin = make_plugin()
        self.use_request(
            method="POST",
            json_data={
                "settings": {
                    "ttsProvider": "openai",
                    "providers": {"openai": {"apiKey": "sk-new"}},
                }
            },
        )
        response = self.call(plugin._web_api.handle_settings)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(plugin._provider(), "openai")
        self.assertEqual(plugin._provider_cfg("openai")["apiKey"], "sk-new")
        self.assertTrue((plugin._data_dir / "settings.json").is_file())

    def test_post_settings_rejects_unknown_provider(self):
        plugin = make_plugin()
        self.use_request(method="POST", json_data={"settings": {"ttsProvider": "nope"}})
        response = self.call(plugin._web_api.handle_settings)
        self.assertEqual(response.status_code, 400)
        self.assertIn("未知", response.message)

    def test_upload_voice_and_duplicate_name(self):
        plugin = make_plugin()
        upload = FakeWebUploadFile("ref.wav", b"RIFF-fake-wav")
        self.use_request(method="POST", files_data={"file": upload})
        response = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["voice"]["name"], "ref")
        self.assertEqual(len(plugin._store().list_voices()), 1)

        same = FakeWebUploadFile("ref.wav", b"RIFF-fake-wav-2")
        self.use_request(method="POST", files_data={"file": same})
        second = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(second.data["voice"]["name"], "ref (2)")

    def test_upload_rejects_bad_extension_and_oversize(self):
        plugin = make_plugin()
        bad = FakeWebUploadFile("note.txt", b"hello")
        self.use_request(method="POST", files_data={"file": bad})
        response = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("不支持", response.message)

        with mock.patch.object(WEB, "MAX_VOICE_BYTES", 4):
            big = FakeWebUploadFile("big.wav", b"12345678")
            self.use_request(method="POST", files_data={"file": big})
            response = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("上限", response.message)

    def test_upload_filename_traversal_is_sanitized(self):
        plugin = make_plugin()
        upload = FakeWebUploadFile("..\\..\\evil.wav", b"RIFF-evil")
        self.use_request(method="POST", files_data={"file": upload})
        response = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(response.status_code, 200)
        entry = response.data["voice"]
        self.assertTrue(entry["file"].endswith(".wav"))
        self.assertNotIn("..", entry["file"])
        self.assertNotIn("/", entry["file"])
        self.assertTrue((plugin._store().root / entry["file"]).is_file())
        self.assertFalse((plugin._store().root.parent / "evil.wav").exists())

    def test_upload_missing_file_field(self):
        plugin = make_plugin()
        self.use_request(method="POST", files_data={})
        response = self.call(plugin._web_api.handle_voice_upload)
        self.assertEqual(response.status_code, 400)
        self.assertIn("file", response.message)

    def test_voice_list_import_delete_and_audio(self):
        plugin = make_plugin()
        ref = write_wav(self.tmp / "server_ref.wav", seconds=0.1)
        self.use_request(method="POST", json_data={"path": str(ref), "name": "服务器导入"})
        imported = self.call(plugin._web_api.handle_voice_import)
        self.assertEqual(imported.status_code, 200)
        entry = imported.data["voice"]

        self.use_request(method="GET")
        listed = self.call(plugin._web_api.handle_voices)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.data["voices"]), 1)
        self.assertEqual(listed.data["voices"][0]["name"], "服务器导入")

        self.use_request(method="GET", query={"name": entry["id"]})
        audio = self.call(plugin._web_api.handle_voice_audio)
        self.assertTrue(Path(audio.path).is_file())
        self.assertEqual(audio.content_type, "audio/wav")

        self.use_request(method="POST", json_data={"id": entry["id"]})
        deleted = self.call(plugin._web_api.handle_voice_delete)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(plugin._store().list_voices(), [])

    def test_voice_audio_blocks_path_traversal(self):
        plugin = make_plugin()
        self.use_request(method="GET", query={"name": "../settings.json"})
        response = self.call(plugin._web_api.handle_voice_audio)
        self.assertEqual(response.status_code, 404)

    def test_import_requires_path(self):
        plugin = make_plugin()
        self.use_request(method="POST", json_data={"name": "x"})
        response = self.call(plugin._web_api.handle_voice_import)
        self.assertEqual(response.status_code, 400)
        self.assertIn("绝对路径", response.message)

    def _install_fake_synth(self):
        async def fake(client, provider, cfg, *, text, ref_path, emotion, speed, dest, timeout):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            write_wav(Path(dest), channels=1, sample_rate=22050, seconds=0.1, freq=440, amp=0.2)
            return {"provider": provider, "path": Path(dest), "format": "wav", "emotionApplied": True}

        return fake

    def test_preview_returns_chunked_audio(self):
        plugin = make_plugin()
        plugin._store().add(write_wav(self.tmp / "ref.wav", seconds=0.1), "小鲸鱼")
        self.use_request(
            method="POST",
            json_data={"text": "你好", "provider": "indextts", "emotion": "happy"},
        )
        with mock.patch.object(TTS, "synthesize", self._install_fake_synth()):
            response = self.call(plugin._web_api.handle_test)
        self.assertEqual(response.status_code, 200)
        payload = response.data
        self.assertTrue(payload.get("previewId"))
        self.assertGreaterEqual(int(payload.get("chunks") or 0), 1)
        collected = b""
        for index in range(int(payload["chunks"])):
            self.use_request(
                method="GET",
                query={"id": payload["previewId"], "index": str(index)},
            )
            chunk = self.call(plugin._web_api.handle_test_chunk)
            self.assertEqual(chunk.status_code, 200)
            collected += base64.b64decode(chunk.data["data"])
        self.assertEqual(len(collected), int(payload["bytes"]))
        self.assertTrue(
            collected.startswith(b"RIFF")
            or collected.startswith(b"ID3")
            or collected[:2] == b"\xff\xfb"
        )

    def test_preview_validates_input(self):
        plugin = make_plugin()
        self.use_request(method="POST", json_data={"text": ""})
        self.assertEqual(self.call(plugin._web_api.handle_test).status_code, 400)
        self.use_request(method="POST", json_data={"text": "你好", "provider": "nope"})
        self.assertEqual(self.call(plugin._web_api.handle_test).status_code, 400)
        self.use_request(method="POST", json_data={"text": "啊" * 301, "provider": "openai"})
        self.assertEqual(self.call(plugin._web_api.handle_test).status_code, 400)

    def test_worker_health_and_process(self):
        plugin = make_plugin()

        async def fake_health():
            return {"ok": True, "version": "1.0.0", "gpu": {"name": "RTX"}}

        self.use_request(method="GET")
        with mock.patch.object(
            MAIN.VoiceStudioPlugin,
            "worker_health",
            new=mock.AsyncMock(side_effect=fake_health),
        ):
            response = self.call(plugin._web_api.handle_worker_health)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["health"]["version"], "1.0.0")

        self.use_request(method="POST", json_data={"process": "rvc-webui", "action": "start"})
        fake_api_json = mock.AsyncMock(return_value={"pid": 123})
        with mock.patch.object(WORKER, "api_json", new=fake_api_json):
            response = self.call(plugin._web_api.handle_worker_process)
        self.assertEqual(response.status_code, 200)
        self.assertIn("rvc-webui", fake_api_json.call_args.args[2])
        self.assertIn("/start", fake_api_json.call_args.args[2])

        self.use_request(method="POST", json_data={"process": "shell", "action": "start"})
        response = self.call(plugin._web_api.handle_worker_process)
        self.assertEqual(response.status_code, 400)


# ---------------------------------------------------------------------------
# 15. 设置页静态资源
# ---------------------------------------------------------------------------


class TestViews(SmokeBase):
    def test_view_files_exist_and_are_referenced(self):
        view_dir = PLUGIN_DIR / "views" / "studio"
        html_path = view_dir / "index.html"
        js_path = view_dir / "app.js"
        css_path = view_dir / "style.css"
        self.assertTrue(html_path.is_file(), "缺少 views/studio/index.html")
        self.assertTrue(js_path.is_file(), "缺少 views/studio/app.js")
        self.assertTrue(css_path.is_file(), "缺少 views/studio/style.css")
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("AstrBotPluginView", html)
        self.assertIn("<style>", html)
        self.assertNotIn("preview-audio", html)
        for element_id in ("provider-select", "provider-fields", "voice-list", "worker-fields", "worker-refresh-models"):
            self.assertIn(f'id="{element_id}"', html)

    def test_app_js_uses_documented_bridge_and_endpoints(self):
        source = (PLUGIN_DIR / "views" / "studio" / "app.js").read_text(encoding="utf-8")
        for token in (
            "window.AstrBotPluginView",
            'bridge.apiGet("settings")',
            'bridge.apiGet("voices")',
            'bridge.apiPost("settings"',
            'bridge.upload("voices/upload"',
            'bridge.apiPost("voices/import"',
            'bridge.apiPost("voices/delete"',
            'bridge.apiGet("worker/health")',
            'bridge.apiPost("worker/process"',
        ):
            self.assertIn(token, source)
        self.assertNotIn("fetch(", source, "页面不应绕过 bridge 直接 fetch 后端")
        self.assertNotIn("XMLHttpRequest", source)

    def test_i18n_metadata_for_view(self):
        for locale in ("zh-CN", "en-US"):
            path = PLUGIN_DIR / ".astrbot-plugin" / "i18n" / f"{locale}.json"
            self.assertTrue(path.is_file(), f"缺少 {locale}.json")
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("title", data["views"]["studio"])
            self.assertIn("description", data["views"]["studio"])

    def test_schema_marks_page_managed_fields_invisible(self):
        schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
        for key in ("ttsProvider", "indextts", "gptsovits", "siliconflow", "minimax", "mimo", "openai"):
            self.assertTrue(schema[key].get("invisible"), f"{key} 应为 invisible")
        for key in ("workerUrl", "workerToken", "workerModel", "workerIndex", "workerF0Method", "workerKeyShift"):
            self.assertTrue(schema[key].get("invisible"), f"{key} 应为 invisible")
        for key in ("allowedGroups", "maxTextLen", "maxDurationSec", "sendProgress", "ttsSpeed", "defaultVoice"):
            self.assertFalse(schema[key].get("invisible"), f"{key} 应保持可见")
        text = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("RVC WebUI", text)


class TestRandomVoice(SmokeBase):
    def test_settings_random_fields_are_validated(self):
        patch = SETTINGS_MOD.validate_settings_patch(
            {
                "randomVoiceEnabled": "true",
                "randomVoiceProbability": 1.5,
                "randomVoiceMaxChars": 5,
            }
        )
        self.assertTrue(patch["randomVoiceEnabled"])
        self.assertEqual(patch["randomVoiceProbability"], 1.0)
        self.assertEqual(patch["randomVoiceMaxChars"], 10)

    def test_merge_settings_random_defaults(self):
        merged = SETTINGS_MOD.merge_settings({}, {})
        self.assertFalse(merged["randomVoiceEnabled"])
        self.assertAlmostEqual(merged["randomVoiceProbability"], 0.15)
        self.assertEqual(merged["randomVoiceMaxChars"], 300)

    def test_random_voice_replaces_plain_text_reply(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {
                "randomVoiceEnabled": True,
                "randomVoiceProbability": 1.0,
                "randomVoiceMaxChars": 80,
            }
        )
        plugin._tmp_dir().mkdir(parents=True, exist_ok=True)
        result = types.SimpleNamespace(chain=[FakePlain("你好")])
        event = FakeEvent("hi", result=result)

        async def fake_synthesize(text, **kwargs):
            dest = plugin._tmp_dir() / "random_voice.wav"
            write_wav(dest, channels=1, sample_rate=22050, seconds=0.1, freq=440, amp=0.2)
            return {"path": dest, "format": "wav"}

        with mock.patch.object(MAIN.VoiceStudioPlugin, "synthesize_to_file", side_effect=fake_synthesize):
            self.run_async(plugin._maybe_random_voice(event))
        self.assertEqual(len(result.chain), 1)
        self.assertIsInstance(result.chain[0], MAIN.Record)

    def test_random_voice_skips_commands(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {
                "randomVoiceEnabled": True,
                "randomVoiceProbability": 1.0,
                "randomVoiceMaxChars": 80,
            }
        )
        result = types.SimpleNamespace(chain=[FakePlain("/朗读 你好")])
        event = FakeEvent("/朗读 你好", result=result)
        self.run_async(plugin._maybe_random_voice(event))
        self.assertIsInstance(result.chain[0], FakePlain)


    def test_takeover_rewrites_official_tts_record(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {
                "randomVoiceEnabled": True,
                "randomVoiceTakeover": True,
                "randomVoiceProbability": 0.0,
            }
        )
        plugin._tmp_dir().mkdir(parents=True, exist_ok=True)
        official = MAIN.Record(file="/official/tts.wav", path="/official/tts.wav")
        result = types.SimpleNamespace(chain=[official])
        event = FakeEvent("hi", result=result)
        setattr(event, "_voice_studio_llm_text", "你好呀")

        async def fake_synthesize(text, **kwargs):
            dest = plugin._tmp_dir() / "takeover.wav"
            write_wav(dest, channels=1, sample_rate=22050, seconds=0.1, freq=440, amp=0.2)
            return {"path": dest, "format": "wav"}

        with mock.patch.object(MAIN.VoiceStudioPlugin, "synthesize_to_file", side_effect=fake_synthesize):
            self.run_async(plugin._maybe_random_voice(event))
        self.assertEqual(len(result.chain), 1)
        self.assertIsInstance(result.chain[0], MAIN.Record)
        self.assertNotEqual(result.chain[0].path, "/official/tts.wav")

    def test_takeover_falls_back_on_synthesis_failure(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {
                "randomVoiceEnabled": True,
                "randomVoiceTakeover": True,
            }
        )
        official = MAIN.Record(file="/official/tts.wav", path="/official/tts.wav")
        result = types.SimpleNamespace(chain=[official])
        event = FakeEvent("hi", result=result)
        setattr(event, "_voice_studio_llm_text", "你好呀")

        async def boom(text, **kwargs):
            raise RuntimeError("tts down")

        with mock.patch.object(MAIN.VoiceStudioPlugin, "synthesize_to_file", side_effect=boom):
            self.run_async(plugin._maybe_random_voice(event))
        self.assertIs(result.chain[0], official)

    def test_capture_llm_text(self):
        plugin = make_plugin()
        event = FakeEvent("hi")
        response = types.SimpleNamespace(completion_text="捕获的回复")
        self.run_async(plugin.capture_llm_text(event, response))
        self.assertEqual(getattr(event, "_voice_studio_llm_text"), "捕获的回复")

    def test_voice_hint_injected_when_enabled(self):
        plugin = make_plugin()
        plugin.save_settings_payload({"randomVoiceEnabled": True})
        event = FakeEvent("hi")
        request = types.SimpleNamespace(system_prompt="你是助手。")
        self.run_async(plugin.inject_voice_hint(event, request))
        self.assertIn("[语音工坊]", request.system_prompt)
        self.assertIn("不要回答", request.system_prompt)

    def test_voice_hint_not_injected_when_disabled(self):
        plugin = make_plugin()
        plugin.save_settings_payload({"randomVoiceEnabled": False})
        event = FakeEvent("hi")
        request = types.SimpleNamespace(system_prompt="你是助手。")
        self.run_async(plugin.inject_voice_hint(event, request))
        self.assertNotIn("[语音工坊]", request.system_prompt)

    def test_split_for_tts_chunks_long_text(self):
        text = "第一句话需要合成语音。" * 8
        chunks = MAIN.split_for_tts(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 30)
        self.assertIn("第一句话需要合成语音。", "".join(chunks))

    def test_concat_wavs(self):
        plugin = make_plugin()
        plugin._tmp_dir().mkdir(parents=True, exist_ok=True)
        first = write_wav(plugin._tmp_dir() / "a.wav", seconds=0.1)
        second = write_wav(plugin._tmp_dir() / "b.wav", seconds=0.1)
        joined = plugin._concat_wavs([first, second])
        with wave.open(str(joined), "rb") as handle:
            self.assertAlmostEqual(handle.getnframes() / handle.getframerate(), 0.2, places=1)

    def test_long_takeover_synthesizes_in_chunks(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {"randomVoiceEnabled": True, "randomVoiceTakeover": True}
        )
        plugin._tmp_dir().mkdir(parents=True, exist_ok=True)
        text = "这一句话比较长需要切分成多段来合成。" * 4
        result = types.SimpleNamespace(chain=[FakePlain(text)])
        event = FakeEvent("hi", result=result)
        calls = []

        async def fake_synthesize(chunk, **kwargs):
            calls.append(chunk)
            dest = plugin._tmp_dir() / ("chunk_%s.wav" % len(calls))
            write_wav(dest, channels=1, sample_rate=22050, seconds=0.1, freq=440, amp=0.2)
            return {"path": dest, "format": "wav"}

        with mock.patch.object(MAIN.VoiceStudioPlugin, "synthesize_to_file", side_effect=fake_synthesize):
            self.run_async(plugin._maybe_random_voice(event))
        self.assertGreater(len(calls), 1)
        self.assertIsInstance(result.chain[0], MAIN.Record)

    def test_random_voice_skips_singing_reply(self):
        plugin = make_plugin()
        plugin.save_settings_payload(
            {"randomVoiceEnabled": True, "randomVoiceTakeover": True}
        )
        result = types.SimpleNamespace(chain=[FakePlain("我来给你唱首歌吧")])
        event = FakeEvent("唱首歌", result=result)
        calls = []

        async def fake_synthesize(text, **kwargs):
            calls.append(text)
            raise AssertionError("singing intent must not go through TTS")

        with mock.patch.object(MAIN.VoiceStudioPlugin, "synthesize_to_file", side_effect=fake_synthesize):
            self.run_async(plugin._maybe_random_voice(event))
        self.assertEqual(calls, [])
        self.assertIsInstance(result.chain[0], FakePlain)

    def test_voice_hint_mentions_rvc_singing(self):
        plugin = make_plugin()
        plugin.save_settings_payload({"randomVoiceEnabled": True})
        event = FakeEvent("hi")
        request = types.SimpleNamespace(system_prompt="你是助手。")
        self.run_async(plugin.inject_voice_hint(event, request))
        self.assertIn("/唱歌", request.system_prompt)

    def test_message_audio_source_local_file(self):
        path = write_wav(self.tmp / "song.wav", seconds=0.1)
        record_type = type("Record", (), {})
        component = record_type()
        component.file = str(path)
        event = FakeEvent("")
        event.message_obj = types.SimpleNamespace(message=[component])
        self.assertEqual(str(MAIN._message_audio_source(event)), str(path))

    def test_message_audio_source_url(self):
        file_type = type("File", (), {})
        component = file_type()
        component.file = ""
        component.url = "https://example.com/song.mp3"
        event = FakeEvent("")
        event.message_obj = types.SimpleNamespace(message=[component])
        self.assertEqual(MAIN._message_audio_source(event), "https://example.com/song.mp3")

    def test_cover_usage_asks_for_song(self):
        plugin = make_plugin()
        event, results = self.run_handler(plugin, "cover_cmd", "")
        joined = " ".join(str(item[1]) for item in results if isinstance(item, tuple))
        self.assertIn("再发送 /唱歌", joined)

    def test_song_listener_asks_for_song(self):
        plugin = make_plugin()
        event = FakeEvent("唱首海屿你")
        self.run_async(plugin.song_listener(event))
        self.assertTrue(event.stopped)
        self.assertEqual(len(event.sent), 1)
        self.assertIn("歌曲音频", str(event.sent[0][1]))

    def test_song_listener_covers_uploaded_song(self):
        plugin = make_plugin()
        song = write_wav(self.tmp / "song.wav", seconds=0.1)
        record_type = type("Record", (), {})
        component = record_type()
        component.file = str(song)
        event = FakeEvent("")
        event.message_obj = types.SimpleNamespace(message=[component])
        plugin._pending_song["default"] = 9_999_999_999.0

        async def fake_cover(event_obj, song_path, model):
            return write_wav(self.tmp / "cover_out.wav", seconds=0.1)

        with mock.patch.object(MAIN.VoiceStudioPlugin, "_run_cover_job", side_effect=fake_cover):
            self.run_async(plugin.song_listener(event))
        texts = [str(item[1]) for item in event.sent if isinstance(item, tuple)]
        self.assertTrue(any("收到" in text for text in texts), texts)
        self.assertTrue(any("chain" in str(item[0]) for item in event.sent if isinstance(item, tuple)))

def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
