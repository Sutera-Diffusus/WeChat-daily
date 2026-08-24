"""Local WeChat voice decoding and Doubao ASR 2.0 integration.

The module deliberately has no dependency on the bridge service or its
database.  It accepts the SILK bytes already obtained by an adapter, converts
them to a stable WAV representation, and optionally submits that WAV to the
Doubao recording-file ASR 2.0 API.

The cloud client uses the legacy-console credentials requested by the project:
``X-Api-App-Key`` and ``X-Api-Access-Key``.  The credentials are kept in
memory only by this module and are never included in logs, request bodies, or
exception text.
"""

from __future__ import annotations

import ast

import base64
import io
import json
import re
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Iterable, Mapping, Optional, Protocol, Union
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SILK_V3_HEADER = b"#!SILK_V3"
TENCENT_SILK_V3_HEADER = b"\x02#!SILK_V3"
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
DEFAULT_SAMPLE_WIDTH = 2

DOUBAO_RESOURCE_ID = "volc.seedasr.auc"
DOUBAO_SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
DOUBAO_QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DOUBAO_SUCCESS_CODE = "20000000"
DOUBAO_PENDING_CODES = frozenset(("20000001", "20000002"))


AudioInput = Union[bytes, bytearray, memoryview, str, Path, BinaryIO]


class VoiceError(RuntimeError):
    """Base class for local voice-pipeline failures."""


class VoiceDecodeError(VoiceError):
    """Raised when a SILK stream cannot be decoded into WAV."""


class ASRError(VoiceError):
    """Base class for safe, non-secret-bearing ASR errors."""


class ASRTransportError(ASRError):
    """Raised when an HTTP request cannot be completed."""


class ASRTimeoutError(ASRError):
    """Raised when polling exceeds the configured overall timeout."""


def _read_bytes(value: AudioInput, label: str) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (str, Path)):
        try:
            return Path(value).expanduser().read_bytes()
        except OSError as exc:
            raise VoiceError("无法读取%s文件" % label) from exc
    reader = getattr(value, "read", None)
    if callable(reader):
        data = reader()
        if not isinstance(data, bytes):
            raise TypeError("%s文件对象必须返回 bytes" % label)
        return data
    raise TypeError("不支持的%s输入类型" % label)


def _load_silk_backend() -> Any:
    """Load the maintained ``silk-python`` backend lazily.

    The PyPI package is named ``silk-python`` but exposes the ``pysilk``
    module.  ``silk`` is accepted as a compatibility fallback for older local
    installations.  Lazy loading keeps the bridge importable when an
    installation has not enabled voice decoding yet.
    """

    try:
        import pysilk  # type: ignore

        return pysilk
    except ImportError:
        try:
            import silk  # type: ignore

            return silk
        except ImportError as exc:
            raise VoiceDecodeError(
                "未安装 SILK 解码器，请安装 silk-python（导入名 pysilk）"
            ) from exc


def _decode_with_backend(backend: Any, silk_bytes: bytes) -> bytes:
    decoder = getattr(backend, "decode", None)
    if not callable(decoder):
        raise VoiceDecodeError("SILK 解码器缺少 decode 接口")
    source = io.BytesIO(silk_bytes)
    pcm = io.BytesIO()
    try:
        decoder(source, pcm, DEFAULT_SAMPLE_RATE)
    except Exception as exc:  # backend exceptions vary by silk-python version
        raise VoiceDecodeError("SILK v3 音频解码失败") from exc
    decoded = pcm.getvalue()
    if not decoded:
        raise VoiceDecodeError("SILK 解码结果为空")
    if len(decoded) % DEFAULT_SAMPLE_WIDTH:
        raise VoiceDecodeError("SILK 解码结果不是完整的 16-bit PCM")
    return decoded


def _pcm_to_wav(pcm: bytes) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav_file:
        wav_file.setnchannels(DEFAULT_CHANNELS)
        wav_file.setsampwidth(DEFAULT_SAMPLE_WIDTH)
        wav_file.setframerate(DEFAULT_SAMPLE_RATE)
        wav_file.writeframes(pcm)
    return output.getvalue()


def decode_silk_to_wav(
    source: AudioInput,
    destination: Optional[Union[str, Path, BinaryIO]] = None,
    *,
    backend: Optional[Any] = None,
) -> bytes:
    """Decode Tencent SILK v3 into 16 kHz mono 16-bit PCM WAV bytes.

    ``source`` may be SILK bytes, a path, or a binary file object.  When
    ``destination`` is supplied the same WAV bytes are also written to that
    path/file object.  The function returns the WAV bytes in both cases so the
    ASR client can submit them directly as ``audio.data``.
    """

    silk_bytes = _read_bytes(source, "SILK")
    if not silk_bytes.startswith((SILK_V3_HEADER, TENCENT_SILK_V3_HEADER)):
        raise VoiceDecodeError("输入不是 Tencent SILK v3 音频")

    pcm = _decode_with_backend(backend or _load_silk_backend(), silk_bytes)
    wav_bytes = _pcm_to_wav(pcm)
    if destination is not None:
        if isinstance(destination, (str, Path)):
            path = Path(destination).expanduser()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(wav_bytes)
            except OSError as exc:
                raise VoiceError("无法写入 WAV 文件") from exc
        else:
            writer = getattr(destination, "write", None)
            if not callable(writer):
                raise TypeError("WAV 输出必须是路径或二进制文件对象")
            writer(wav_bytes)
    return wav_bytes


def decode_silk_file(source: Union[str, Path], destination: Union[str, Path]) -> Path:
    """Decode a SILK file and return the destination path."""

    target = Path(destination).expanduser()
    decode_silk_to_wav(source, target)
    return target


@dataclass(frozen=True)
class HttpResponse:
    """Small transport-neutral HTTP response used by the fake test transport."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes = b""

    def json(self) -> Dict[str, Any]:
        if not self.body:
            return {}
        try:
            value = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}


class HttpTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> HttpResponse:
        ...


class UrllibTransport:
    """Default stdlib transport; tests can inject a fake transport instead."""

    def post(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout: float,
    ) -> HttpResponse:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with urlopen(request, timeout=timeout) as response:
                return HttpResponse(
                    int(response.status),
                    {str(key): str(value) for key, value in response.headers.items()},
                    response.read(),
                )
        except HTTPError as exc:
            headers_value = {
                str(key): str(value) for key, value in (exc.headers or {}).items()
            }
            try:
                response_body = exc.read()
            except OSError:
                response_body = b""
            return HttpResponse(int(exc.code), headers_value, response_body)
        except (OSError, URLError, TimeoutError) as exc:
            raise ASRTransportError("豆包 ASR 网络请求失败") from exc


@dataclass(frozen=True)
class ASRTask:
    """An accepted async ASR task."""

    task_id: str
    submit_response: Mapping[str, Any]


@dataclass(frozen=True)
class TranscriptionResult:
    """Normalized ASR result while retaining the provider response for audit."""

    task_id: str
    text: str
    raw: Mapping[str, Any]
    utterances: tuple
    duration_ms: Optional[int] = None


def extract_wechat_voice_transcript(value: Any) -> str:
    """Recover WeChat's own voice-to-text result from packed_info_data."""

    if value is None:
        return ""
    data = value
    if isinstance(data, str) and data.startswith(("b'", 'b"')):
        try:
            data = ast.literal_eval(data)
        except (SyntaxError, ValueError):
            data = data.encode("utf-8", errors="ignore")
    elif isinstance(data, str):
        data = data.encode("utf-8", errors="ignore")
    if not isinstance(data, (bytes, bytearray)):
        return ""
    decoded = bytes(data).decode("utf-8", errors="ignore")
    candidates = [part.strip() for part in re.findall(r"[^\x00-\x1f\x7f]{4,}", decoded)]
    candidates = [part for part in candidates if any("\u4e00" <= char <= "\u9fff" for char in part)]
    return re.sub(r"X$", "", max(candidates, key=len, default="")).strip()


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _response_body(response: Any) -> Dict[str, Any]:
    if isinstance(response, HttpResponse):
        return response.json()
    body = getattr(response, "body", None)
    if isinstance(body, bytes):
        return HttpResponse(0, {}, body).json()
    if isinstance(body, str):
        return HttpResponse(0, {}, body.encode("utf-8")).json()
    json_method = getattr(response, "json", None)
    if callable(json_method):
        try:
            value = json_method()
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _coerce_response(value: Any) -> HttpResponse:
    if isinstance(value, HttpResponse):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        status, headers, body = value
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HttpResponse(int(status), dict(headers), bytes(body or b""))
    status = int(getattr(value, "status_code", getattr(value, "status", 0)))
    headers = dict(getattr(value, "headers", {}) or {})
    body = getattr(value, "content", getattr(value, "body", b""))
    if callable(body):
        body = body()
    if isinstance(body, str):
        body = body.encode("utf-8")
    return HttpResponse(status, headers, bytes(body or b""))


def _safe_message(message: Any, secrets: Iterable[str]) -> str:
    text = str(message or "")
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(
        r"(?i)(x-api-(?:app|access)-key|api[-_ ]?key|access[-_ ]?token|secret[-_ ]?key)"
        r"\s*[:=]\s*([^,;\s]+)",
        r"\1=<redacted>",
        text,
    )
    text = re.sub(r"[\r\n\t]+", " ", text).strip()
    return text[:240]


class DoubaoASRClient:
    """Client for Doubao recording-file recognition model 2.0.

    The standard API uses a client-generated request ID as the async task ID:
    Submit sends the Base64 WAV in ``audio.data``; Query sends an empty JSON
    object with the same ``X-Api-Request-Id`` until status ``20000000``.
    """

    def __init__(
        self,
        app_key: Optional[str] = None,
        access_key: Optional[str] = None,
        *,
        app_id: Optional[str] = None,
        access_token: Optional[str] = None,
        resource_id: str = DOUBAO_RESOURCE_ID,
        submit_url: str = DOUBAO_SUBMIT_URL,
        query_url: str = DOUBAO_QUERY_URL,
        transport: Optional[HttpTransport] = None,
        request_timeout: float = 30.0,
        poll_interval: float = 2.0,
        poll_timeout: float = 300.0,
        sleep: Callable[[float], None] = time.sleep,
        uid: Optional[str] = None,
    ) -> None:
        self._app_key = str(app_key if app_key is not None else app_id or "").strip()
        self._access_key = str(
            access_key if access_key is not None else access_token or ""
        ).strip()
        if not self._app_key or not self._access_key:
            raise ValueError("豆包 ASR 需要 App Key 和 Access Key")
        if not resource_id:
            raise ValueError("豆包 ASR Resource ID 不能为空")
        if request_timeout <= 0 or poll_timeout <= 0:
            raise ValueError("ASR 超时时间必须大于 0")
        if poll_interval < 0:
            raise ValueError("ASR 轮询间隔不能小于 0")
        self._resource_id = resource_id
        self._submit_url = submit_url
        self._query_url = query_url
        self._transport = transport or UrllibTransport()
        self._request_timeout = float(request_timeout)
        self._poll_interval = float(poll_interval)
        self._poll_timeout = float(poll_timeout)
        self._sleep = sleep
        self._uid = str(uid or "wechat-bridge")

    def _headers(self, request_id: str, include_sequence: bool) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Key": self._app_key,
            "X-Api-Access-Key": self._access_key,
            "X-Api-Resource-Id": self._resource_id,
            "X-Api-Request-Id": request_id,
        }
        if include_sequence:
            headers["X-Api-Sequence"] = "-1"
        return headers

    def _post(
        self,
        url: str,
        payload: Mapping[str, Any],
        request_id: str,
        phase: str,
    ) -> HttpResponse:
        try:
            response = _coerce_response(
                self._transport.post(
                    url,
                    headers=self._headers(request_id, include_sequence=phase == "submit"),
                    payload=payload,
                    timeout=self._request_timeout,
                )
            )
        except ASRError:
            raise
        except Exception as exc:
            raise ASRTransportError("豆包 ASR %s 请求失败" % phase) from exc

        status_code = _header(response.headers, "X-Api-Status-Code")
        body = _response_body(response)
        body_code = str(body.get("code", "") or body.get("status_code", ""))
        code = status_code or body_code
        message = _header(response.headers, "X-Api-Message") or body.get("message", "")
        if response.status_code < 200 or response.status_code >= 300:
            raise ASRRequestError(
                "豆包 ASR %s 请求失败" % phase,
                http_status=response.status_code,
                code=code,
                message=message,
                request_id=request_id,
                secrets=(self._app_key, self._access_key),
            )
        if phase == "submit" and code and code != DOUBAO_SUCCESS_CODE:
            raise ASRRequestError(
                "豆包 ASR Submit 被拒绝",
                http_status=response.status_code,
                code=code,
                message=message,
                request_id=request_id,
                secrets=(self._app_key, self._access_key),
            )
        if phase == "query" and code and code not in (
            DOUBAO_SUCCESS_CODE,
            *DOUBAO_PENDING_CODES,
        ):
            raise ASRRequestError(
                "豆包 ASR Query 失败",
                http_status=response.status_code,
                code=code,
                message=message,
                request_id=request_id,
                secrets=(self._app_key, self._access_key),
            )
        return response

    def submit(
        self,
        audio: AudioInput,
        *,
        audio_format: str = "wav",
        uid: Optional[str] = None,
        request_id: Optional[str] = None,
        request_options: Optional[Mapping[str, Any]] = None,
    ) -> ASRTask:
        audio_bytes = _read_bytes(audio, "音频")
        if not audio_bytes:
            raise ValueError("不能提交空音频")
        task_id = str(request_id or uuid.uuid4())
        request_config: Dict[str, Any] = {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
        }
        if request_options:
            request_config.update(dict(request_options))
        payload = {
            "user": {"uid": str(uid or self._uid)},
            "audio": {
                "format": str(audio_format),
                "data": base64.b64encode(audio_bytes).decode("ascii"),
            },
            "request": request_config,
        }
        response = self._post(self._submit_url, payload, task_id, "submit")
        return ASRTask(task_id=task_id, submit_response=_response_body(response))

    def _query(self, task_id: str) -> HttpResponse:
        return self._post(self._query_url, {}, task_id, "query")

    def poll(
        self,
        task_id: str,
        *,
        poll_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> TranscriptionResult:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise ValueError("ASR task ID 不能为空")
        overall_timeout = self._poll_timeout if poll_timeout is None else float(poll_timeout)
        interval = self._poll_interval if poll_interval is None else float(poll_interval)
        if overall_timeout <= 0 or interval < 0:
            raise ValueError("ASR 轮询超时或间隔参数无效")
        deadline = time.monotonic() + overall_timeout
        last_code = ""
        while True:
            response = self._query(task_id)
            body = _response_body(response)
            last_code = _header(response.headers, "X-Api-Status-Code") or str(
                body.get("code", "") or body.get("status_code", "")
            )
            if last_code == DOUBAO_SUCCESS_CODE or _has_result(body):
                return _transcription_result(task_id, body)
            if last_code not in DOUBAO_PENDING_CODES and body:
                raise ASRRequestError(
                    "豆包 ASR Query 未返回可用结果",
                    http_status=response.status_code,
                    code=last_code,
                    message=_header(response.headers, "X-Api-Message"),
                    request_id=task_id,
                    secrets=(self._app_key, self._access_key),
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ASRTimeoutError("豆包 ASR 轮询超时")
            self._sleep(min(interval, remaining))

    def transcribe(
        self,
        audio: AudioInput,
        *,
        audio_format: str = "wav",
        uid: Optional[str] = None,
        request_options: Optional[Mapping[str, Any]] = None,
        poll_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> TranscriptionResult:
        task = self.submit(
            audio,
            audio_format=audio_format,
            uid=uid,
            request_options=request_options,
        )
        return self.poll(
            task.task_id,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )


class ASRRequestError(ASRError):
    """Provider/API error whose text is deliberately redacted."""

    def __init__(
        self,
        context: str,
        *,
        http_status: Optional[int] = None,
        code: str = "",
        message: str = "",
        request_id: str = "",
        secrets: Iterable[str] = (),
    ) -> None:
        self.http_status = http_status
        self.code = _safe_message(code, secrets)
        self.request_id = _safe_message(request_id, secrets)
        self.provider_message = _safe_message(message, secrets)
        details = [_safe_message(context, secrets)]
        if self.provider_message:
            details.append(self.provider_message)
        if http_status is not None:
            details.append("http=%s" % http_status)
        if self.code:
            details.append("code=%s" % self.code)
        if self.request_id:
            details.append("request_id=%s" % self.request_id)
        super().__init__("；".join(details))


def _has_result(body: Mapping[str, Any]) -> bool:
    result = body.get("result")
    if not isinstance(result, Mapping):
        data = body.get("data")
        result = data.get("result") if isinstance(data, Mapping) else None
    return isinstance(result, Mapping) and "text" in result


def _transcription_result(task_id: str, body: Mapping[str, Any]) -> TranscriptionResult:
    data = body.get("data")
    data = data if isinstance(data, Mapping) else body
    result = data.get("result")
    result = result if isinstance(result, Mapping) else {}
    text = str(result.get("text") or "")
    utterances = result.get("utterances")
    if not isinstance(utterances, list):
        utterances = []
    audio_info = data.get("audio_info")
    audio_info = audio_info if isinstance(audio_info, Mapping) else {}
    duration = audio_info.get("duration")
    if duration is None:
        additions = result.get("additions")
        if isinstance(additions, Mapping):
            duration = additions.get("duration")
    try:
        duration_ms = int(duration) if duration is not None else None
    except (TypeError, ValueError):
        duration_ms = None
    return TranscriptionResult(
        task_id=task_id,
        text=text,
        raw=dict(body),
        utterances=tuple(utterances),
        duration_ms=duration_ms,
    )


class VoicePipeline:
    """Convenience composition of SILK decoding followed by ASR."""

    def __init__(self, asr_client: DoubaoASRClient, silk_backend: Optional[Any] = None):
        self.asr_client = asr_client
        self.silk_backend = silk_backend

    def transcribe_silk(
        self,
        source: AudioInput,
        *,
        uid: Optional[str] = None,
        request_options: Optional[Mapping[str, Any]] = None,
        poll_timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> TranscriptionResult:
        wav_bytes = decode_silk_to_wav(source, backend=self.silk_backend)
        return self.asr_client.transcribe(
            wav_bytes,
            audio_format="wav",
            uid=uid,
            request_options=request_options,
            poll_timeout=poll_timeout,
            poll_interval=poll_interval,
        )
