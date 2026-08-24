import base64
import io
import json
import wave

import pytest

from wechat_bridge.voice import (
    ASRTimeoutError,
    DOUBAO_RESOURCE_ID,
    DoubaoASRClient,
    HttpResponse,
    VoiceDecodeError,
    decode_silk_to_wav,
)


class FakeSilkBackend:
    def decode(self, source, output, sample_rate):
        assert sample_rate == 16_000
        assert source.read() == b"\x02#!SILK_V3fixture"
        output.write(b"\x01\x00\x02\x00" * 20)


class FakeHTTPTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.last_response = None

    def post(self, url, *, headers, payload, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": payload,
                "timeout": timeout,
            }
        )
        if self.responses:
            self.last_response = self.responses.pop(0)
        return self.last_response


def _response(code, body=None, message="OK", status=200):
    headers = {"X-Api-Status-Code": code, "X-Api-Message": message}
    encoded = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    return HttpResponse(status, headers, encoded)


def test_tencent_silk_is_decoded_to_16khz_mono_wav():
    wav_bytes = decode_silk_to_wav(
        b"\x02#!SILK_V3fixture",
        backend=FakeSilkBackend(),
    )

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 16_000
        assert wav_file.readframes(wav_file.getnframes()) == b"\x01\x00\x02\x00" * 20


def test_invalid_silk_header_is_rejected():
    with pytest.raises(VoiceDecodeError):
        decode_silk_to_wav(b"not-silk", backend=FakeSilkBackend())


def test_doubao_submit_query_uses_base64_and_redacts_credentials():
    result_body = {
        "audio_info": {"duration": 2060},
        "result": {
            "text": "请确认选课时间。",
            "utterances": [{"text": "请确认选课时间。", "confidence": 92}],
        },
    }
    transport = FakeHTTPTransport(
        [
            _response("20000000"),
            _response("20000001", {}),
            _response("20000000", result_body),
        ]
    )
    client = DoubaoASRClient(
        app_key="app-secret-value",
        access_key="access-secret-value",
        transport=transport,
        poll_interval=0,
        poll_timeout=1,
        sleep=lambda _seconds: None,
    )

    transcription = client.transcribe(b"wav-fixture", poll_interval=0)

    assert transcription.text == "请确认选课时间。"
    assert transcription.duration_ms == 2060
    assert transcription.utterances[0]["confidence"] == 92
    assert len(transport.calls) == 3
    submit = transport.calls[0]
    assert submit["url"].endswith("/submit")
    assert submit["headers"]["X-Api-App-Key"] == "app-secret-value"
    assert submit["headers"]["X-Api-Access-Key"] == "access-secret-value"
    assert submit["headers"]["X-Api-Resource-Id"] == DOUBAO_RESOURCE_ID
    assert submit["headers"]["X-Api-Sequence"] == "-1"
    assert submit["payload"]["audio"]["data"] == base64.b64encode(b"wav-fixture").decode()
    assert submit["payload"]["request"]["model_name"] == "bigmodel"
    query = transport.calls[1]
    assert query["url"].endswith("/query")
    assert query["payload"] == {}
    assert query["headers"]["X-Api-Request-Id"] == submit["headers"]["X-Api-Request-Id"]


def test_doubao_poll_timeout_is_safe_and_does_not_expose_keys():
    transport = FakeHTTPTransport(
        [_response("20000000"), _response("20000001", {})]
    )
    client = DoubaoASRClient(
        app_key="visible-only-in-header-app",
        access_key="visible-only-in-header-access",
        transport=transport,
        poll_interval=0,
        poll_timeout=0.00001,
        sleep=lambda _seconds: None,
    )
    task = client.submit(b"wav-fixture")

    with pytest.raises(ASRTimeoutError) as caught:
        client.poll(task.task_id, poll_timeout=0.00001, poll_interval=0)

    message = str(caught.value)
    assert "visible-only-in-header-app" not in message
    assert "visible-only-in-header-access" not in message


def test_doubao_http_error_message_is_redacted():
    transport = FakeHTTPTransport(
        [
            _response(
                "40100000",
                {"message": "access-visible-in-header-access"},
                message="invalid access-visible-in-header-access",
                status=401,
            )
        ]
    )
    client = DoubaoASRClient(
        app_key="app-visible-in-header-app",
        access_key="access-visible-in-header-access",
        transport=transport,
    )

    with pytest.raises(Exception) as caught:
        client.submit(b"wav-fixture")

    assert "app-visible-in-header-app" not in str(caught.value)
    assert "access-visible-in-header-access" not in str(caught.value)
