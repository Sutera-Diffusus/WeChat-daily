import http.client
import json
import sys
import struct
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from wechat_bridge.adapters.base import WeChatAdapter
from wechat_bridge.ai import OpenAIAnalysisGenerator
from wechat_bridge.engine import ReplyPolicy
from wechat_bridge.media import V1_MAGIC, decode_dat
from wechat_bridge.models import HealthStatus, IncomingMessage, ReplyDecision, SendResult
from wechat_bridge.service import BridgeService
from wechat_bridge.settings import WorkbenchSettings
from wechat_bridge.store import SQLiteStore
from wechat_bridge.web import BridgeRequestHandler, start_dashboard_thread


class SettingsAdapter(WeChatAdapter):
    name = "settings-test"

    @property
    def version(self):
        return "test"

    def connect(self):
        pass

    def disconnect(self):
        pass

    def health_check(self):
        return HealthStatus(True, self.name, self.version, "ok")

    def start_receive(self, chat_names, callback):
        self.callback = callback

    def stop_receive(self):
        self.callback = None

    def send_text(self, chat_id, chat_name, content):
        return SendResult(True, True, "confirmed")

    def get_chat_history(self, chat_id, chat_name="", limit=50):
        return []


def _request(server, method, path, body=None):
    connection = http.client.HTTPConnection(
        "127.0.0.1", server.server_address[1], timeout=3
    )
    headers = {}
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.headers, response.read()
    finally:
        connection.close()


def _service(tmp):
    store = SQLiteStore(str(Path(tmp) / "bridge.db"))
    service = BridgeService(
        SettingsAdapter(),
        store,
        ReplyPolicy.from_values("不会发送", ("文件传输助手",)),
        ("文件传输助手",),
        dry_run=True,
        send_enabled=False,
    )
    service.start()
    return service, store


def test_settings_public_payload_masks_secrets_and_persists(tmp_path):
    settings = WorkbenchSettings(str(tmp_path / "workbench_settings.json"))

    value = settings.update(
        {
            "display": {"font_size": "large", "density": "comfortable"},
            "refresh": {"enabled": False, "interval_ms": 1},
            "ai": {
                "base_url": "https://api.example.test/v1/",
                "model": "vision-model",
                "api_key": "sk-test-secret-1234",
                "clear_api_key": False,
            },
        }
    )

    assert value["display"] == {
        "font_size": "large",
        "density": "comfortable",
        "report_theme": "auto",
    }
    assert value["refresh"]["enabled"] is False
    assert value["refresh"]["interval_ms"] == 3_000
    assert value["ai"]["base_url"] == "https://api.example.test/v1"
    assert value["ai"]["api_key_configured"] is True
    assert "api_key" not in value["ai"]
    assert "clear_api_key" not in json.loads(
        (tmp_path / "workbench_settings.json").read_text(encoding="utf-8")
    )["ai"]

    reloaded = WorkbenchSettings(str(tmp_path / "workbench_settings.json"))
    assert reloaded.public()["ai"]["api_key_masked"] == "sk-t••••1234"

    reloaded.update({"ai": {"api_key": ""}})
    assert reloaded.public()["ai"]["api_key_configured"] is True

    reloaded.update({"ai": {"clear_api_key": True}})
    assert reloaded.public()["ai"]["api_key_configured"] is False


def test_analysis_profile_and_voice_settings_are_bounded_and_masked(tmp_path):
    settings = WorkbenchSettings(str(tmp_path / "workbench_settings.json"))
    value = settings.update(
        {
            "analysis": {"interval_ms": 10, "message_threshold": 0},
            "profile": {"projects": ["选课", "选课", "微信工作台"]},
            "voice": {
                "enabled": True,
                "app_id": "test-app",
                "access_token": "voice-access-token",
                "secret_key": "voice-secret-key",
            },
        }
    )

    assert value["analysis"]["interval_ms"] == 60_000
    assert value["analysis"]["message_threshold"] == 1
    assert value["profile"]["projects"] == ["选课", "微信工作台"]
    assert value["voice"]["access_token_configured"] is True
    assert value["voice"]["secret_key_configured"] is True
    assert "access_token" not in value["voice"]
    assert "secret_key" not in value["voice"]


def test_settings_http_endpoint_is_local_and_returns_public_shape(tmp_path):
    service, store = _service(tmp_path)
    server, _thread = start_dashboard_thread(service, port=0)
    try:
        status, _headers, body = _request(server, "GET", "/api/settings")
        value = json.loads(body.decode("utf-8"))
        assert status == 200
        assert value["refresh"]["enabled"] is True
        assert "api_key" not in value["ai"]

        status, _headers, body = _request(
            server,
            "POST",
            "/api/settings",
            {
                "refresh": {"interval_ms": 15_000},
                "media": {"cache_dir": str(tmp_path / "cache")},
                "ai": {"base_url": "https://api.example.test", "api_key": "local-key"},
            },
        )
        value = json.loads(body.decode("utf-8"))
        assert status == 200
        assert value["ok"] is True
        assert value["settings"]["refresh"]["interval_ms"] == 15_000
        assert value["settings"]["ai"]["api_key_configured"] is True
        assert "local-key" not in body.decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        store.close()


def test_media_endpoint_serves_indexed_local_image_and_caches_copy(tmp_path):
    service, store = _service(tmp_path)
    server, _thread = start_dashboard_thread(service, port=0)
    try:
        cache_dir = tmp_path / "cache"
        status, _headers, _body = _request(
            server,
            "POST",
            "/api/settings",
            {"media": {"cache_dir": str(cache_dir)}},
        )
        assert status == 200

        source = cache_dir / "sample.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        image_bytes = b"\xff\xd8\xff\x00minimal-test-image"
        source.write_bytes(image_bytes)
        message = IncomingMessage(
            message_id="image-1",
            chat_id="filehelper",
            chat_name="文件传输助手",
            sender_id="sender",
            sender_name="测试联系人",
            message_type="image",
            content="[图片]",
            timestamp=datetime.now(timezone.utc),
            is_self=False,
            raw_message={"path": str(source)},
            adapter_name="settings-test",
            adapter_version="test",
            media_path=str(source),
            media_name="sample.jpg",
        )
        store.ingest(message, ReplyDecision(False, "media fixture"), create_task=False)

        status, headers, body = _request(
            server, "GET", "/api/media?message_id=image-1"
        )
        assert status == 200
        assert headers.get("Content-Type") == "image/jpeg"
        assert body == image_bytes
        cached = list(cache_dir.glob("*.jpg"))
        assert len(cached) >= 1
    finally:
        server.shutdown()
        server.server_close()
        service.stop()
        store.close()


def test_v1_dat_image_decodes_with_fixed_header_and_xor_tail(tmp_path):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    head = b"\xff\xd8\xff"
    tail = b"tail-bytes"
    padder = padding.PKCS7(128).padder()
    padded = padder.update(head) + padder.finalize()
    cipher = Cipher(algorithms.AES(b"cfcd208495d565ef"), modes.ECB())
    encryptor = cipher.encryptor()
    encrypted_head = encryptor.update(padded) + encryptor.finalize()
    xor_key = 0xA1
    encrypted_tail = bytes(value ^ xor_key for value in tail)
    raw = (
        V1_MAGIC
        + struct.pack("<I", len(head))
        + struct.pack("<I", len(tail))
        + b"\x00"
        + encrypted_head
        + encrypted_tail
    )
    path = tmp_path / "v1.dat"
    path.write_bytes(raw)

    data, extension, content_type = decode_dat(path, xor_key=xor_key)

    assert data == head + tail
    assert extension == "jpg"
    assert content_type == "image/jpeg"


def test_ai_generator_uses_chat_completions_for_custom_base_url(monkeypatch):
    calls = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls.update(kwargs)
            payload = {
                "brief": "发现一个需要人工确认的事项。",
                "themes": ["项目"],
                "findings": [],
                "limitations": [],
            }
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    class FakeOpenAI:
        def __init__(self, api_key, base_url=None):
            calls["api_key"] = api_key
            calls["base_url"] = base_url
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    generator = OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="deepseek-flash",
        reasoning_effort="max",
    )
    result = generator.analyze(
        {"start": "2026-08-21", "end": "2026-08-21"},
        [
            {
                "evidence_ref": "m-001",
                "timestamp": "2026-08-21T01:00:00+00:00",
                "chat_name": "项目群",
                "sender_name": "群成员",
                "content": "请确认方案",
                "rule_signals": ["待办"],
                "rule_level": "high",
            }
        ],
    )

    assert result["brief"].startswith("发现")
    assert calls["api_key"] == "test-key"
    assert calls["base_url"] == "https://api.deepseek.com"
    assert calls["model"] == "deepseek-flash"
    assert calls["reasoning_effort"] == "max"
    assert calls["max_tokens"] == 65536
    assert calls["response_format"] == {"type": "json_object"}


def test_ai_generator_packet_mode_uses_smaller_medium_reasoning_call(monkeypatch):
    calls = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls.update(kwargs)
            payload = {"brief": "", "findings": [], "limitations": []}
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
            )

    class FakeOpenAI:
        def __init__(self, api_key, base_url=None):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    result = OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        reasoning_effort="max",
        max_tokens=65536,
    ).analyze(
        {"start": "2026-08-21", "end": "2026-08-21"},
        [{"evidence_ref": "m-001", "content": "请确认方案"}],
        packet_mode=True,
    )

    assert result["findings"] == []
    assert calls["reasoning_effort"] == "medium"
    assert calls["max_tokens"] == 16384
    assert "\"claims\"" in calls["messages"][1]["content"]
    instructions = calls["messages"][0]["content"]
    assert "庄重平实、准确凝练" in instructions
    assert "每条 claim 必须保留明确发言人姓名" in instructions
    assert "不得使用有人、群友、成员等无主表述" in instructions


def test_ai_generator_global_prompt_requires_authoritative_style_and_explicit_attribution(monkeypatch):
    calls = {}

    class FakeCompletions:
        def create(self, **kwargs):
            calls.update(kwargs)
            message = SimpleNamespace(content=json.dumps({
                "brief": "",
                "situation": "",
                "key_changes": [],
                "themes": [],
                "open_questions": [],
                "timeline": [],
                "findings": [],
                "limitations": [],
            }, ensure_ascii=False))
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")])

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
    ).analyze(
        {"start": "2026-09-08", "end": "2026-09-08"},
        [{"evidence_ref": "m-1", "sender_name": "张三", "content": "方案已经完成测试"}],
    )

    instructions = calls["messages"][0]["content"]
    assert "《人民日报》《新华社》《先锋》《文汇》" in instructions
    assert "庄重平实、鲜活有力" in instructions
    assert "剔除空话、套话与重复冗余" in instructions
    assert "谁说了什么必须保留明确归属" in instructions
    assert "不得使用有人、群友、成员等无主表述替代已知姓名" in instructions


def test_ai_generator_accumulates_safe_usage_across_empty_retry(monkeypatch):
    calls = 0

    class FakeCompletions:
        def create(self, **_kwargs):
            nonlocal calls
            calls += 1
            content = "" if calls == 1 else json.dumps(
                {"brief": "完成", "findings": [], "limitations": []}
            )
            usage = SimpleNamespace(
                prompt_tokens=100,
                prompt_cache_hit_tokens=80 if calls == 2 else 0,
                prompt_cache_miss_tokens=20 if calls == 2 else 100,
                completion_tokens=10,
                completion_tokens_details=SimpleNamespace(reasoning_tokens=6),
            )
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content=content, reasoning_content=""),
                    finish_reason="stop",
                )],
                usage=usage,
            )

    class FakeOpenAI:
        def __init__(self, api_key, base_url=None):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    result = OpenAIAnalysisGenerator(
        api_key="test-key", base_url="https://api.deepseek.com"
    ).analyze(
        {"start": "2026-08-21", "end": "2026-08-21"},
        [{"evidence_ref": "m-001", "content": "请确认方案"}],
    )

    assert result["_usage"] == {
        "prompt_tokens": 200,
        "input_tokens": 200,
        "prompt_cache_hit_tokens": 80,
        "prompt_cache_miss_tokens": 120,
        "completion_tokens": 20,
        "output_tokens": 20,
        "reasoning_tokens": 12,
        "attempts": 2,
        "retries": 1,
    }


def test_ai_generator_stage_clone_applies_packet_effort_and_budget():
    source = OpenAIAnalysisGenerator(
        model="deepseek-flash",
        api_key="test-key",
        base_url="https://api.deepseek.com",
        reasoning_effort="max",
        max_tokens=65536,
    )

    packet = BridgeRequestHandler._ai_generator_with_effort(source, "low", 8192)

    assert packet.reasoning_effort == "low"
    assert packet.max_tokens == 8192
    assert packet.packet_reasoning_effort == "low"
    assert packet.packet_max_tokens == 8192


def test_ai_generator_environment_overrides_model_and_normalizes_reasoning(monkeypatch):
    monkeypatch.setenv("OPENAI_WECHAT_ANALYSIS_MODEL", "deepseek-flash")
    monkeypatch.setenv("OPENAI_WECHAT_ANALYSIS_REASONING_EFFORT", "MAX")
    settings = SimpleNamespace(
        snapshot=lambda **_kwargs: {
            "ai": {
                "model": "persisted-model",
                "api_key": "test-key",
                "base_url": "https://api.deepseek.com",
            }
        }
    )
    handler = SimpleNamespace(server=SimpleNamespace(settings=settings))

    generator = BridgeRequestHandler._ai_generator(handler)

    assert generator.model == "deepseek-flash"
    assert generator.reasoning_effort == "max"


def test_ai_generator_ignores_invalid_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_WECHAT_ANALYSIS_REASONING_EFFORT", "turbo")
    settings = SimpleNamespace(
        snapshot=lambda **_kwargs: {
            "ai": {"model": "persisted-model", "api_key": "test-key"}
        }
    )
    handler = SimpleNamespace(server=SimpleNamespace(settings=settings))

    generator = BridgeRequestHandler._ai_generator(handler)

    assert generator.reasoning_effort is None


def test_ai_analysis_max_tokens_defaults_and_explicit_override():
    regular = OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
    )
    maximum_reasoning = OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        reasoning_effort="max",
    )
    explicit = OpenAIAnalysisGenerator(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        reasoning_effort="max",
        max_tokens=49152,
    )

    assert regular.max_tokens == 32768
    assert maximum_reasoning.max_tokens == 65536
    assert explicit.max_tokens == 49152


def test_ai_generator_max_tokens_environment_override_is_safely_bounded(monkeypatch):
    settings = SimpleNamespace(
        snapshot=lambda **_kwargs: {
            "ai": {"model": "persisted-model", "api_key": "test-key"}
        }
    )
    handler = SimpleNamespace(server=SimpleNamespace(settings=settings))

    monkeypatch.setenv("OPENAI_WECHAT_ANALYSIS_MAX_TOKENS", "49152")
    assert BridgeRequestHandler._ai_generator(handler).max_tokens == 49152

    monkeypatch.setenv("OPENAI_WECHAT_ANALYSIS_MAX_TOKENS", "999999")
    assert BridgeRequestHandler._ai_generator(handler).max_tokens == 32768
