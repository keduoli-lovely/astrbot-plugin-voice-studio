"""语音工坊 AstrBot 插件的冒烟测试（本机没有 AstrBot，全部用 mock）。

运行：
    python tests/smoke.py

覆盖：
1. 在伪 AstrBot（Star / filter / 事件 / 消息组件）环境下能 import main.py；
2. 4 个中文命令被 @filter.command 注册（含 /语音说 别名）；
3. HTTP 客户端按配置构造：httpx.AsyncClient、超时、Bearer 头、URL 归一化；
4. /朗读 真的 POST 到 `/tts-studio/api/tts`，参数名是 voiceId / sendToQQ=false；
5. TTS 返回远程 url 时能流式下载（跨机部署回退路径）；
6. 群白名单拦截；
7. /音色、/语音状态 的输出；
8. WAV 混音（RMS 对齐 + 峰值归一化）与 /翻唱 全链路（fake worker）。

注意：脚本会把伪造的 `astrbot` / `httpx` 模块写进 sys.modules，只影响本进程。
"""

from __future__ import annotations

import asyncio
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
MAIN_PATH = PLUGIN_DIR / "main.py"


# ---------------------------------------------------------------------------
# 伪造 AstrBot / httpx
# ---------------------------------------------------------------------------


class FakeConfig(dict):
    """AstrBotConfig 是 dict 子类，这里只用到字典行为。"""


class FakeLogger:
    def __init__(self):
        self.lines: list[str] = []

    def debug(self, message, *args):
        self.lines.append(("debug", str(message) % args if args else str(message)))

    def info(self, message, *args):
        self.lines.append(("info", str(message) % args if args else str(message)))

    def warning(self, message, *args):
        self.lines.append(("warning", str(message) % args if args else str(message)))

    def error(self, message, *args):
        self.lines.append(("error", str(message) % args if args else str(message)))

    def exception(self, message, *args):
        self.lines.append(("exception", str(message) % args if args else str(message)))


class FakeRecord:
    """伪造 astrbot.api.message_components.Record。"""

    def __init__(self, file: str = "", url: str = "", path: str = "", **_):
        self.file = file
        self.url = url
        self.path = path

    @classmethod
    def fromFileSystem(cls, path):
        return cls(file=f"file:///{Path(path).as_posix().lstrip('/')}", path=str(path))


class FakeStar:
    def __init__(self, context):
        self.context = context


class FakeContext:
    pass


class FakeEvent:
    """伪造 AstrMessageEvent：命令 handler 用到的接口都在这里。"""

    def __init__(self, message_str: str = "", group_id: str = ""):
        self.message_str = message_str
        self._group_id = str(group_id or "")
        self.stopped = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return "10000"

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


class _FakeTimeout:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeHTTPResponse:
    def __init__(self, status_code: int = 200, payload=None, body: bytes = b""):
        self.status_code = status_code
        self._payload = payload
        self._body = body

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload

    async def aread(self):
        return self._body

    async def aiter_bytes(self, chunk_size: int = 65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]


class _FakeStream:
    def __init__(self, response: FakeHTTPResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *_exc):
        return False


class FakeAsyncClient:
    """伪造 httpx.AsyncClient，记录请求；响应由测试里的 handler 决定。"""

    instances: list["FakeAsyncClient"] = []
    request_handler = None  # type: ignore[assignment]
    stream_handler = None  # type: ignore[assignment]

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list[dict] = []
        self.streams: list[dict] = []
        self.closed = False
        FakeAsyncClient.instances.append(self)

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, "kwargs": kwargs})
        handler = FakeAsyncClient.request_handler
        if handler is None:
            return FakeHTTPResponse(200, {})
        return handler(method, url, kwargs)

    def stream(self, method, url, **kwargs):
        self.streams.append({"method": method, "url": url, "kwargs": kwargs})
        handler = FakeAsyncClient.stream_handler
        response = FakeHTTPResponse(200, {}, b"") if handler is None else handler(method, url, kwargs)
        return _FakeStream(response)

    async def aclose(self):
        self.closed = True


class FakeHTTPError(Exception):
    pass


def install_fake_modules():
    """把伪造的 astrbot / httpx 注入 sys.modules。"""

    filter_namespace = FakeFilter()

    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    api_mod.AstrBotConfig = FakeConfig
    api_mod.logger = FakeLogger()
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = FakeEvent
    event_mod.filter = filter_namespace
    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Record = FakeRecord
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = FakeContext
    star_mod.Star = FakeStar

    httpx_mod = types.ModuleType("httpx")
    httpx_mod.AsyncClient = FakeAsyncClient
    httpx_mod.Timeout = _FakeTimeout
    httpx_mod.HTTPError = FakeHTTPError

    sys.modules.update(
        {
            "astrbot": astrbot_mod,
            "astrbot.api": api_mod,
            "astrbot.api.event": event_mod,
            "astrbot.api.message_components": components_mod,
            "astrbot.api.star": star_mod,
            "httpx": httpx_mod,
        }
    )
    return filter_namespace


def load_main_module():
    spec = importlib.util.spec_from_file_location("astrbot_plugin_voice_studio_main", str(MAIN_PATH))
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"无法加载 {MAIN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def write_wav(path: Path, *, channels: int, sample_rate: int, seconds: float, freq: float, amp: float) -> Path:
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


class SmokeBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.filter_namespace = install_fake_modules()
        cls.main = load_main_module()
        cls.tmp = Path(tempfile.mkdtemp(prefix="vs_smoke_"))

    def make_plugin(self, **overrides):
        config = {
            "voiceStudioUrl": "127.0.0.1:9999",
            "voiceStudioToken": "voice-token",
            "workerUrl": "http://127.0.0.1:7870/",
            "workerToken": "worker-token",
            "defaultProvider": "indextts",
            "defaultVoice": "voice-1",
            "defaultEmotion": "happy:0.8",
            "allowedGroups": [],
            "maxDurationSec": 600,
            "maxTextLen": 1000,
            "jobTimeoutSec": 60,
            "requestTimeoutSec": 60,
            "sendProgress": True,
            "workerModel": "test.pth",
            "workerIndex": "",
            "workerF0Method": "rmvpe",
            "workerKeyShift": 3,
        }
        config.update(overrides)
        return self.main.VoiceStudioPlugin(FakeContext(), config)

    async def collect(self, generator):
        results = []
        async for item in generator:
            results.append(item)
        return results


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestImportsAndRegistration(SmokeBase):
    def test_import_paths_are_the_documented_ones(self):
        # main.py 顶部 import 走的是文档里的 astrbot.api.* 路径；能 import 即说明路径正确。
        self.assertTrue(hasattr(self.main, "VoiceStudioPlugin"))
        self.assertTrue(issubclass(self.main.VoiceStudioPlugin, FakeStar))
        self.assertIs(self.main.Record, FakeRecord)
        self.assertEqual(self.main.PLUGIN_NAME, "astrbot_plugin_voice_studio")

    def test_commands_registered(self):
        registered = {
            name: alias
            for name, alias in self.filter_namespace.commands
        }
        for expected in ("朗读", "翻唱", "音色", "语音状态"):
            self.assertIn(expected, registered, f"命令 {expected} 未注册")
        self.assertIn("语音说", registered["朗读"], "朗读 应带 语音说 别名")
        # handler 确实挂在类上
        for attr in ("tts_cmd", "cover_cmd", "voices_cmd", "status_cmd"):
            self.assertTrue(callable(getattr(self.main.VoiceStudioPlugin, attr, None)), attr)


class TestHelpers(SmokeBase):
    def test_command_arg(self):
        arg = self.main._command_arg
        self.assertEqual(arg("/朗读 你好 世界"), "你好 世界")
        self.assertEqual(arg("朗读 你好"), "你好")
        self.assertEqual(arg("语音说 晚安"), "晚安")
        self.assertEqual(arg("/语音状态"), "")
        self.assertEqual(arg("音色"), "")

    def test_split_cover_arg(self):
        split = self.main._split_cover_arg
        self.assertEqual(split('"C:\\music\\a b.mp3"'), ("C:\\music\\a b.mp3", ""))
        self.assertEqual(split('"C:\\music\\a b.mp3" model.pth'), ("C:\\music\\a b.mp3", "model.pth"))
        self.assertEqual(split("C:\\music\\a.mp3"), ("C:\\music\\a.mp3", ""))

    def test_normalize_emotion(self):
        normalize = self.main.VoiceStudioPlugin._normalize_emotion
        self.assertIsNone(normalize("auto"))
        self.assertIsNone(normalize(""))
        self.assertEqual(normalize("happy"), "happy")
        self.assertEqual(normalize("happy:0.8"), {"label": "happy", "intensity": 0.8})

    def test_config_helpers_tolerate_junk(self):
        self.assertEqual(self.main._conf_int({"a": "12"}, "a", 0), 12)
        self.assertEqual(self.main._conf_int({"a": "oops"}, "a", 7), 7)
        self.assertTrue(self.main._conf_bool({"a": "true"}, "a", False))
        self.assertEqual(self.main._conf_list({"a": "1,2"}, "a", []), ["1", "2"])


class TestHttpClient(SmokeBase):
    def test_client_construction_and_headers(self):
        plugin = self.make_plugin()
        FakeAsyncClient.instances.clear()

        async def scenario():
            client = await plugin._get_vs_client()
            http_client = await client._get_client()  # 底层 httpx.AsyncClient（已 mock）
            await client.close()
            return client, http_client

        client, http_client = asyncio.run(scenario())
        self.assertIsInstance(http_client, FakeAsyncClient)
        self.assertEqual(http_client.kwargs["follow_redirects"], True)
        self.assertIsInstance(http_client.kwargs["timeout"], _FakeTimeout)
        self.assertEqual(client.voice_base, "http://127.0.0.1:9999")
        self.assertEqual(client.worker_base, "http://127.0.0.1:7870")
        self.assertEqual(client.voice_headers()["Authorization"], "Bearer voice-token")
        self.assertEqual(client.worker_headers()["Authorization"], "Bearer worker-token")
        self.assertTrue(http_client.closed)

    def test_missing_url_raises_chinese_error(self):
        plugin = self.make_plugin(voiceStudioUrl="")
        client = plugin._build_client()
        with self.assertRaises(self.main.VoiceStudioError) as ctx:
            _ = client.voice_base
        self.assertIn("voiceStudioUrl", str(ctx.exception))


class TestTtsCommand(SmokeBase):
    def setUp(self):
        super().setUp()
        self.audio = write_wav(
            self.tmp / "tts.wav", channels=1, sample_rate=24000, seconds=0.2, freq=440, amp=0.2
        )
        FakeAsyncClient.instances.clear()

    def _run_tts(self, plugin, message_str="/朗读 你好 世界", group_id=""):
        event = FakeEvent(message_str, group_id)

        async def scenario():
            return await self.collect(plugin.tts_cmd(event))

        return event, asyncio.run(scenario())

    def test_tts_posts_documented_payload_and_sends_record(self):
        plugin = self.make_plugin()

        def handler(method, url, kwargs):
            return FakeHTTPResponse(
                200,
                {"ok": True, "file": str(self.audio), "format": "wav", "provider": "indextts"},
            )

        with mock.patch.object(FakeAsyncClient, "request_handler", staticmethod(handler)):
            _event, results = self._run_tts(plugin)

        client = FakeAsyncClient.instances[-1]
        request = client.requests[-1]
        self.assertEqual(request["method"], "POST")
        self.assertEqual(request["url"], "http://127.0.0.1:9999/tts-studio/api/tts")
        body = request["kwargs"]["json"]
        self.assertEqual(body["text"], "你好 世界")
        self.assertIs(body["sendToQQ"], False)
        self.assertEqual(body["voiceId"], "voice-1")
        self.assertEqual(body["provider"], "indextts")
        self.assertEqual(body["emotion"], {"label": "happy", "intensity": 0.8})
        self.assertEqual(request["kwargs"]["headers"]["Authorization"], "Bearer voice-token")
        self.assertEqual(len(results), 1)
        kind, chain = results[0]
        self.assertEqual(kind, "chain")
        self.assertIsInstance(chain[0], FakeRecord)
        self.assertEqual(chain[0].path, str(self.audio))

    def test_tts_remote_url_is_streamed_when_file_not_local(self):
        plugin = self.make_plugin()

        def handler(method, url, kwargs):
            return FakeHTTPResponse(
                200,
                {"ok": True, "file": "D:/elsewhere/va.wav", "format": "wav",
                 "url": "/tts-studio/api/audio?path=va.wav"},
            )

        def stream_handler(method, url, kwargs):
            return FakeHTTPResponse(200, None, b"RIFF-fake-wav-bytes")

        with mock.patch.object(FakeAsyncClient, "request_handler", staticmethod(handler)), \
                mock.patch.object(FakeAsyncClient, "stream_handler", staticmethod(stream_handler)):
            _event, results = self._run_tts(plugin)

        client = FakeAsyncClient.instances[-1]
        self.assertTrue(client.streams, "跨机部署时应回落到 url 流式下载")
        self.assertEqual(client.streams[-1]["url"], "http://127.0.0.1:9999/tts-studio/api/audio?path=va.wav")
        self.assertEqual(len(results), 1)
        record = results[0][1][0]
        self.assertTrue(Path(record.path).is_file())

    def test_group_whitelist_blocks(self):
        plugin = self.make_plugin(allowedGroups=["111"])
        event, results = self._run_tts(plugin, message_str="/朗读 hi", group_id="222")
        self.assertTrue(event.stopped)
        self.assertEqual(len(results), 1)
        self.assertIn("白名单", results[0][1])
        self.assertEqual(FakeAsyncClient.instances, [], "被白名单拦截时不应该创建 HTTP 客户端")

    def test_group_whitelist_allows_listed_group(self):
        plugin = self.make_plugin(allowedGroups=["111"])
        self.assertIsNone(plugin._check_allowed(FakeEvent("/朗读 hi", "111")))

    def test_empty_text_returns_usage(self):
        plugin = self.make_plugin()
        _event, results = self._run_tts(plugin, message_str="/朗读")
        self.assertIn("用法", results[0][1])


class TestVoicesAndStatus(SmokeBase):
    def test_voices_lists_library(self):
        plugin = self.make_plugin()

        def handler(method, url, kwargs):
            self.assertIn("/tts-studio/api/voices", url)
            return FakeHTTPResponse(
                200,
                {"voices": [{"id": "v1", "name": "小鲸鱼", "durationSec": 8.25, "available": True}]},
            )

        with mock.patch.object(FakeAsyncClient, "request_handler", staticmethod(handler)):
            results = asyncio.run(self.collect(plugin.voices_cmd(FakeEvent("/音色"))))
        self.assertIn("小鲸鱼", results[0][1])
        self.assertIn("v1", results[0][1])
        self.assertIn("参考音色 1 个", results[0][1])

    def test_status_reports_both_services(self):
        plugin = self.make_plugin()

        def handler(method, url, kwargs):
            if "/tts-studio/api/state" in url:
                return FakeHTTPResponse(
                    200,
                    {"ok": True, "settings": {"tts": {"provider": "indextts"}}, "voices": [{"id": "v1"}]},
                )
            if url.endswith("/v1/health"):
                return FakeHTTPResponse(
                    200,
                    {
                        "ok": True,
                        "version": "1.0.0",
                        "rvc": {"available": True, "models": [{"id": "m1"}]},
                        "gpu": {"name": "RTX 4090"},
                        "jobs": {"running": 0},
                    },
                )
            raise AssertionError(url)

        with mock.patch.object(FakeAsyncClient, "request_handler", staticmethod(handler)):
            results = asyncio.run(self.collect(plugin.status_cmd(FakeEvent("/语音状态"))))
        text = results[0][1]
        self.assertIn("✅ 语音工坊", text)
        self.assertIn("✅ rvc-worker", text)
        self.assertIn("indextts", text)
        self.assertIn("RTX 4090", text)


class TestMixAndCover(SmokeBase):
    def test_mix_cover_rms_and_peak(self):
        main = self.main
        inst = write_wav(self.tmp / "inst.wav", channels=2, sample_rate=44100, seconds=0.5, freq=220, amp=0.3)
        vocal = write_wav(self.tmp / "vocal.wav", channels=1, sample_rate=22050, seconds=0.5, freq=440, amp=0.15)
        source = write_wav(self.tmp / "source.wav", channels=1, sample_rate=22050, seconds=0.5, freq=440, amp=0.3)
        out = self.tmp / "mixed.wav"
        main._mix_cover(inst, vocal, source, out)

        self.assertTrue(out.is_file())
        duration = main._wav_duration_sec(out)
        self.assertAlmostEqual(duration, 0.5, places=2)
        decoded = main._decode_wav(out)
        self.assertEqual(decoded["channels"], 2)
        self.assertEqual(decoded["sample_rate"], 44100)
        peak = float(abs(decoded["samples"]).max())
        self.assertAlmostEqual(peak, 0.891, places=2)

    def test_cover_handler_end_to_end_with_fake_worker(self):
        main = self.main
        song = write_wav(self.tmp / "song.wav", channels=2, sample_rate=44100, seconds=0.5, freq=200, amp=0.2)
        vocal = write_wav(self.tmp / "sep_vocal.wav", channels=1, sample_rate=44100, seconds=0.5, freq=300, amp=0.15)
        inst = write_wav(self.tmp / "sep_inst.wav", channels=2, sample_rate=44100, seconds=0.5, freq=200, amp=0.2)
        converted = write_wav(self.tmp / "sep_conv.wav", channels=1, sample_rate=44100, seconds=0.5, freq=310, amp=0.2)

        class FakeWorkerClient(main.VoiceStudioClient):
            def __init__(self):
                super().__init__(voice_base="http://127.0.0.1:1", worker_base="http://127.0.0.1:2")
                self.jobs = []
                self.uploaded = []

            async def upload_file(self, path):
                self.uploaded.append(Path(path))
                return "file-song"

            async def create_job(self, job_type, file_id, params=None):
                self.jobs.append((job_type, file_id, dict(params or {})))
                return f"job-{job_type}"

            async def wait_job(self, job_id):
                if job_id == "job-separate":
                    return {
                        "status": "succeeded",
                        "outputs": [
                            {"name": "vocal.wav", "fileId": "file-vocal"},
                            {"name": "instrumental.wav", "fileId": "file-inst"},
                        ],
                    }
                return {
                    "status": "succeeded",
                    "outputs": [{"name": "converted.wav", "fileId": "file-conv"}],
                }

            async def download_job_output(self, job_id, name, dest):
                source = converted if "convert" in job_id else (vocal if "vocal" in name else inst)
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(source.read_bytes())
                return dest

        plugin = self.make_plugin()
        fake_client = FakeWorkerClient()
        plugin._client = fake_client
        event = FakeEvent(f'/翻唱 "{song}"')

        results = asyncio.run(self.collect(plugin.cover_cmd(event)))

        self.assertEqual([job[0] for job in fake_client.jobs], ["separate", "convert"])
        convert_params = fake_client.jobs[1][2]
        self.assertEqual(convert_params["model"], "test.pth")
        self.assertEqual(convert_params["f0_method"], "rmvpe")
        self.assertEqual(convert_params["f0_up_key"], 3)

        final_kind, final_chain = results[-1]
        self.assertEqual(final_kind, "chain")
        record = final_chain[0]
        self.assertTrue(Path(record.path).is_file())
        self.assertTrue(Path(record.path).name.endswith(".wav"))
        mixed = main._decode_wav(Path(record.path))
        self.assertEqual(mixed["channels"], 2)
        # 混音结果应是可播放的非静音音频
        self.assertGreater(float(abs(mixed["samples"]).max()), 0.5)

    def test_cover_rejects_missing_file(self):
        plugin = self.make_plugin()
        missing = self.tmp / "does_not_exist.mp3"
        results = asyncio.run(self.collect(plugin.cover_cmd(FakeEvent(f"/翻唱 {missing}"))))
        self.assertIn("找不到歌曲文件", results[0][1])


def main():
    unittest.main(verbosity=2)


if __name__ == "__main__":
    main()
