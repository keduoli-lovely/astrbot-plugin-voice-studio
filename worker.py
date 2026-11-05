"""rvc-worker HTTP 客户端（插件唯一的外部重活依赖）。

契约以 ``rvc-worker/API.md`` 为准：
``GET /v1/health``、``GET /v1/models``、``POST /v1/files``、
``POST /v1/jobs``、``GET /v1/jobs/{id}``、``GET /v1/jobs/{id}/output?name=``。
不依赖 DSH 的任何接口。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from urllib.parse import quote

from .common import VoiceStudioError

_JOB_OK = "succeeded"
_JOB_FAILED = {"failed", "canceled", "cancelled"}


def normalize_base(url: str) -> str:
    value = str(url or "").strip()
    if not value:
        raise VoiceStudioError("未配置 workerUrl，请先在 AstrBot 插件配置里填写 rvc-worker 地址。")
    if not re.match(r"^https?://", value, re.IGNORECASE):
        value = "http://" + value
    return value.rstrip("/")


def headers_for(token: str) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    token = str(token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _brief(value: object, size: int = 300) -> str:
    text = str(value or "").strip().replace("\n", " ")
    return text[:size] + ("…" if len(text) > size else "")


def _error_text(url: str, status: int, data: object) -> str:
    if status == 401:
        return (
            "翻唱 worker 连接失败：缺少或无效的 Bearer token（HTTP 401）。"
            "请把设置页「Worker Token」与 worker 启动参数 --token 填成同一个值。"
        )
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or error.get("detail") or "").strip()
            if message and code:
                return f"{message}（{code}, HTTP {status}）"
            if message:
                return f"{message}（HTTP {status}）"
        if isinstance(error, str) and error.strip():
            return f"{error.strip()}（HTTP {status}）"
        if data.get("message"):
            return f"{data['message']}（HTTP {status}）"
    return f"HTTP {status}（{url}）"


async def api_json(client, method: str, url: str, *, token: str, timeout: float = 30.0, **kwargs) -> dict:
    """发一个请求并把非 2xx 转成中文 VoiceStudioError。"""

    try:
        response = await client.request(
            method,
            url,
            headers=headers_for(token),
            timeout=max(5.0, float(timeout)),
            **kwargs,
        )
    except VoiceStudioError:
        raise
    except Exception as exc:  # noqa: BLE001
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


async def download(client, url: str, dest: Path, *, token: str, timeout: float = 600.0) -> Path:
    """流式下载到本地文件（整首歌级别，避免一次性读进内存）。"""

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        async with client.stream(
            "GET",
            url,
            headers=headers_for(token),
            timeout=max(10.0, float(timeout)),
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


async def health(client, base: str, token: str) -> dict:
    return await api_json(client, "GET", f"{base}/v1/health", token=token, timeout=10.0)


async def models(client, base: str, token: str) -> dict:
    return await api_json(client, "GET", f"{base}/v1/models", token=token, timeout=20.0)


async def upload_file(client, base: str, token: str, path: Path) -> str:
    url = f"{base}/v1/files"
    path = Path(path)
    try:
        handle = open(path, "rb")  # noqa: SIM115 - 请求期间交给 httpx 读
    except OSError as exc:
        raise VoiceStudioError(f"读取歌曲文件失败：{exc}") from exc
    try:
        with handle:
            data = await api_json(
                client,
                "POST",
                url,
                token=token,
                timeout=600.0,
                files={"file": (path.name, handle, "application/octet-stream")},
            )
    finally:
        if not handle.closed:
            handle.close()
    file_id = str(data.get("fileId") or "").strip()
    if not file_id:
        raise VoiceStudioError("上传歌曲后 worker 没有返回 fileId")
    return file_id


async def create_job(
    client,
    base: str,
    token: str,
    job_type: str,
    file_id: str,
    params: dict | None = None,
) -> str:
    url = f"{base}/v1/jobs"
    payload = {
        "type": job_type,
        "fileId": file_id,
        "params": {**(params or {}), "fileId": file_id},
    }
    data = await api_json(client, "POST", url, token=token, timeout=60.0, json=payload)
    job_id = str(data.get("jobId") or data.get("id") or "").strip()
    if not job_id:
        raise VoiceStudioError("worker 没有返回 jobId")
    return job_id


async def wait_job(
    client,
    base: str,
    token: str,
    job_id: str,
    *,
    timeout_s: float = 1800.0,
    poll_interval: float = 2.0,
    logger=None,
) -> dict:
    url = f"{base}/v1/jobs/{job_id}"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(60.0, float(timeout_s))
    last_status = ""
    while True:
        data = await api_json(client, "GET", url, token=token, timeout=20.0)
        status = str(data.get("status") or "").strip().lower()
        if status != last_status and logger is not None:
            try:
                logger.debug("worker job %s -> %s (stage=%s)", job_id, status, data.get("stage"))
            except Exception:  # noqa: BLE001
                pass
            last_status = status
        if status == _JOB_OK:
            return data
        if status in _JOB_FAILED:
            detail = data.get("error") or data.get("stage") or "未知原因"
            raise VoiceStudioError(f"worker 任务失败（{status}）：{_brief(detail, 400)}")
        if loop.time() > deadline:
            raise VoiceStudioError(
                f"worker 任务超时（超过 {int(timeout_s)} 秒，job {job_id}）"
            )
        await asyncio.sleep(max(0.2, float(poll_interval)))


async def download_job_output(
    client,
    base: str,
    token: str,
    job_id: str,
    name: str,
    dest: Path,
) -> Path:
    url = f"{base}/v1/jobs/{quote(str(job_id))}/output?name={quote(str(name))}"
    return await download(client, url, dest, token=token)


async def download_file(client, base: str, token: str, file_id: str, dest: Path) -> Path:
    url = f"{base}/v1/files/{quote(str(file_id))}"
    return await download(client, url, dest, token=token)


__all__ = [
    "api_json",
    "create_job",
    "download",
    "download_file",
    "download_job_output",
    "headers_for",
    "health",
    "models",
    "normalize_base",
    "upload_file",
    "wait_job",
]
