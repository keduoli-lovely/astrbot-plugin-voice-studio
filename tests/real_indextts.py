"""真机测试：直接调用插件的 IndexTTS 提供商合成一句中文（不需要 AstrBot）。

前提：
    本机 IndexTTS（Gradio 5.x）在运行，默认 http://127.0.0.1:7860；
    有参考音色文件（默认用 DSH 语音工坊 ~/.dsh-voice-studio/refs 下的第一个 mp3）。
    httpx 不在当前解释器时，脚本会尝试临时目录里的安装：
        python -m pip install --target %TEMP%\\vs_httpx httpx
    （也可以设置环境变量 VS_HTTPX_PATH 指向任意含 httpx 的目录）

运行：
    python tests/real_indextts.py
    python tests/real_indextts.py --base http://127.0.0.1:7860 --text "你好" --ref "C:\\path\\ref.wav"

成功标准：生成文件存在、>10KB、RIFF/WAVE 头合法；打印耗时与文件信息，退出码 0。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PKG = "vs_real_indextts_pkg"


def ensure_httpx():
    try:
        import httpx  # noqa: PLC0415
    except ImportError:
        candidates = [
            os.environ.get("VS_HTTPX_PATH"),
            str(Path(tempfile.gettempdir()) / "vs_httpx"),
        ]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if (path / "httpx").is_dir():
                sys.path.insert(0, str(path))
                break
        import httpx  # noqa: PLC0415, F811

    return httpx


def load_modules():
    """按包上下文加载插件子模块（插件目录名带连字符，不能直接 import）。"""

    for name in list(sys.modules):
        if name == PKG or name.startswith(PKG + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        PKG, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载插件包：{PLUGIN_DIR}")
    package = importlib.util.module_from_spec(spec)
    sys.modules[PKG] = package
    spec.loader.exec_module(package)

    def load(name: str):
        module_spec = importlib.util.spec_from_file_location(f"{PKG}.{name}", PLUGIN_DIR / f"{name}.py")
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[f"{PKG}.{name}"] = module
        module_spec.loader.exec_module(module)
        return module

    tts_providers = load("tts_providers")
    emotion = load("emotion")
    audio = load("audio")
    return tts_providers, emotion, audio


def find_ref_voice(explicit: str | None) -> Path:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise SystemExit(f"[real] 指定的参考音频不存在：{path}")
        return path
    refs_dir = Path.home() / ".dsh-voice-studio" / "refs"
    preferred = refs_dir / "v_d46bc95c07914908.mp3"
    if preferred.is_file():
        return preferred
    if refs_dir.is_dir():
        for candidate in sorted(refs_dir.iterdir()):
            if candidate.is_file() and candidate.suffix.lower() in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
                return candidate
    raise SystemExit(
        "[real] 找不到参考音色：请用 --ref 指定一个本机 wav/mp3 文件"
        f"（也找过 {refs_dir}）。"
    )


def is_riff_wav(path: Path) -> bool:
    try:
        head = path.read_bytes()[:12]
    except OSError:
        return False
    return len(head) >= 12 and head[0:4] == b"RIFF" and head[8:12] == b"WAVE"


async def main_async(args) -> int:
    httpx = ensure_httpx()
    tts_providers, emotion_mod, audio = load_modules()

    base = args.base.rstrip("/")
    ref = find_ref_voice(args.ref)
    out_dir = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="vs_real_indextts_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"indextts_real_{int(time.time())}.wav"

    cfg = {
        "baseUrl": base,
        "lang": args.lang,
        "emoWeight": args.emo_weight,
        "durationFactor": args.duration_factor,
    }
    emo = emotion_mod.resolve_emotion(args.text, args.emotion, mode="auto", default_intensity=0.6)

    print(f"[real] IndexTTS base = {base}")
    print(f"[real] ref audio     = {ref} ({ref.stat().st_size / 1024:.1f}KB)")
    print(f"[real] text          = {args.text}")
    print(f"[real] emotion       = {emotion_mod.describe_emotion(emo)}")
    print(f"[real] output        = {dest}")

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=900.0, write=300.0, pool=10.0),
        follow_redirects=True,
    )
    started = time.perf_counter()
    try:
        result = await tts_providers.synthesize(
            client,
            "indextts",
            cfg,
            text=args.text,
            ref_path=ref,
            emotion=emo,
            speed=1.0,
            dest=dest,
            timeout=900.0,
        )
    finally:
        await client.aclose()
    elapsed = time.perf_counter() - started

    path = Path(result["path"])
    size = path.stat().st_size if path.is_file() else 0
    duration = audio.wav_duration_sec(path) if path.is_file() else None
    print(f"[real] provider      = {result['provider']} / format={result['format']} / emotionApplied={result['emotionApplied']}")
    print(f"[real] file          = {path}")
    print(f"[real] size          = {size} bytes ({size / 1024:.1f}KB)")
    print(f"[real] duration      = {duration if duration is None else f'{duration:.2f}s'}")
    print(f"[real] riff-wav ok   = {is_riff_wav(path)}")
    print(f"[real] elapsed       = {elapsed:.2f}s")

    failures = []
    if not path.is_file():
        failures.append("输出文件不存在")
    if size <= 10 * 1024:
        failures.append(f"输出文件 <=10KB（实际 {size} bytes）")
    if not is_riff_wav(path):
        failures.append("输出不是合法的 RIFF/WAVE")
    if failures:
        print("[real] FAIL: " + "；".join(failures))
        return 1
    print("[real] PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="IndexTTS 真机测试")
    parser.add_argument("--base", default=os.environ.get("INDEXTTS_BASE", "http://127.0.0.1:7860"))
    parser.add_argument(
        "--text",
        default="你好，我是语音工坊，今天天气真好，我们出去走走吧。",
    )
    parser.add_argument("--ref", default=os.environ.get("INDEXTTS_REF"))
    parser.add_argument("--lang", default="ZH")
    parser.add_argument("--emotion", default=None, help="显式情绪，如 happy；默认自动判断")
    parser.add_argument("--emo-weight", type=float, default=0.8)
    parser.add_argument("--duration-factor", type=float, default=1.0)
    parser.add_argument("--out", default=None, help="输出目录")
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - 真机脚本把错误原样打印出来方便排查
        import traceback

        traceback.print_exc()
        print(f"[real] FAIL: {exc.__class__.__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
