"""Persistent, local-only workbench settings.

The dashboard is intentionally a read-only client of the bridge.  These
settings therefore live beside the SQLite archive instead of in browser
storage, so refresh cadence and AI configuration survive a page reload while
remaining local to this machine.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


_FONT_SIZES = {"small", "normal", "large"}
_DENSITIES = {"compact", "comfortable"}
_REPORT_THEMES = {"auto", "classic", "cobalt", "forest"}
_EMAIL_SECURITY = {"ssl", "starttls", "none"}
_MIN_REFRESH_MS = 3_000
_MAX_REFRESH_MS = 300_000
_MIN_ANALYSIS_MS = 60_000
_MAX_ANALYSIS_MS = 86_400_000


def _mask_secret(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "••••••••"
    return text[:4] + "••••" + text[-4:]


class WorkbenchSettings:
    """Thread-safe JSON settings with a deliberately small public surface."""

    def __init__(self, path: str, base_dir: Optional[str] = None) -> None:
        self.path = Path(path).expanduser()
        self.base_dir = Path(base_dir or self.path.parent).expanduser().resolve()
        self._lock = threading.RLock()
        self._data = self._load()

    @classmethod
    def for_service(cls, service: Any) -> "WorkbenchSettings":
        raw_path = str(getattr(getattr(service, "store", None), "path", "") or "")
        if raw_path and raw_path != ":memory:":
            database_path = Path(raw_path).expanduser()
            base_dir = database_path.parent
        else:
            base_dir = Path.cwd() / "data"
        return cls(str(base_dir / "workbench_settings.json"), str(base_dir))

    def _defaults(self) -> Dict[str, Any]:
        return {
            "display": {"font_size": "normal", "density": "compact", "report_theme": "auto"},
            "refresh": {"enabled": True, "interval_ms": 8_000},
            "analysis": {
                "auto_enabled": True,
                "interval_ms": 600_000,
                "message_threshold": 20,
            },
            "media": {
                "cache_dir": str((self.base_dir / "media_cache").resolve()),
                "image_aes_key": "",
                "image_xor_key": None,
            },
            "ai": {
                "base_url": "",
                "model": "gpt-5.2",
                "api_key": "",
            },
            "email": {
                "host": "",
                "port": 465,
                "security": "ssl",
                "username": "",
                "password": "",
                "sender": "",
            },
            "voice": {
                "enabled": False,
                "provider": "doubao_asr_v2",
                "app_id": "",
                "access_token": "",
                "secret_key": "",
                "resource_id": "volc.seedasr.auc",
                "single_duration_threshold_seconds": 20,
                "chat_cumulative_threshold_seconds": 60,
                "low_confidence_threshold": 0.75,
                "keep_source_audio": False,
            },
            "profile": {
                "roles": [],
                "projects": [],
                "organizations": [],
                "key_contacts": [],
                "topics": [],
                "suggestions": [],
            },
        }

    @staticmethod
    def _merge(base: Dict[str, Any], value: Mapping[str, Any]) -> Dict[str, Any]:
        for key, incoming in value.items():
            if isinstance(incoming, Mapping) and isinstance(base.get(key), dict):
                WorkbenchSettings._merge(base[key], incoming)
            else:
                base[key] = incoming
        return base

    def _load(self) -> Dict[str, Any]:
        value = self._defaults()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, Mapping):
                self._merge(value, raw)
        except (OSError, ValueError, TypeError):
            pass
        return self._normalize(value)

    def _normalize(self, value: Dict[str, Any]) -> Dict[str, Any]:
        display = value.setdefault("display", {})
        font_size = str(display.get("font_size") or "normal").strip().lower()
        display["font_size"] = font_size if font_size in _FONT_SIZES else "normal"
        density = str(display.get("density") or "compact").strip().lower()
        display["density"] = density if density in _DENSITIES else "compact"
        report_theme = str(display.get("report_theme") or "auto").strip().lower()
        display["report_theme"] = report_theme if report_theme in _REPORT_THEMES else "auto"

        refresh = value.setdefault("refresh", {})
        refresh["enabled"] = bool(refresh.get("enabled", True))
        try:
            interval = int(refresh.get("interval_ms", 8_000))
        except (TypeError, ValueError):
            interval = 8_000
        refresh["interval_ms"] = max(_MIN_REFRESH_MS, min(_MAX_REFRESH_MS, interval))

        analysis = value.setdefault("analysis", {})
        analysis["auto_enabled"] = bool(analysis.get("auto_enabled", True))
        try:
            analysis_interval = int(analysis.get("interval_ms", 600_000))
        except (TypeError, ValueError):
            analysis_interval = 600_000
        analysis["interval_ms"] = max(_MIN_ANALYSIS_MS, min(_MAX_ANALYSIS_MS, analysis_interval))
        try:
            message_threshold = int(analysis.get("message_threshold", 20))
        except (TypeError, ValueError):
            message_threshold = 20
        analysis["message_threshold"] = max(1, min(1_000, message_threshold))

        media = value.setdefault("media", {})
        cache_dir = str(media.get("cache_dir") or (self.base_dir / "media_cache"))
        cache_path = Path(cache_dir).expanduser()
        if not cache_path.is_absolute():
            cache_path = self.base_dir / cache_path
        media["cache_dir"] = str(cache_path.resolve())
        media["image_aes_key"] = str(media.get("image_aes_key") or "").strip()
        xor_key = media.get("image_xor_key")
        if xor_key in (None, ""):
            media["image_xor_key"] = None
        else:
            try:
                media["image_xor_key"] = max(0, min(255, int(xor_key)))
            except (TypeError, ValueError):
                media["image_xor_key"] = None

        ai = value.setdefault("ai", {})
        base_url = str(ai.get("base_url") or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = ""
        ai["base_url"] = base_url
        ai["model"] = str(ai.get("model") or "gpt-5.2").strip()[:120] or "gpt-5.2"
        ai["api_key"] = str(ai.get("api_key") or "").strip()

        email = value.setdefault("email", {})
        email["host"] = str(email.get("host") or "").strip()[:255]
        try:
            email["port"] = max(1, min(65_535, int(email.get("port", 465))))
        except (TypeError, ValueError):
            email["port"] = 465
        security = str(email.get("security") or "ssl").strip().lower()
        email["security"] = security if security in _EMAIL_SECURITY else "ssl"
        email["username"] = str(email.get("username") or "").strip()[:255]
        email["password"] = str(email.get("password") or "").strip()
        email["sender"] = str(email.get("sender") or "").strip()[:320]

        voice = value.setdefault("voice", {})
        voice["enabled"] = bool(voice.get("enabled", False))
        voice["provider"] = "doubao_asr_v2"
        voice["app_id"] = str(voice.get("app_id") or "").strip()[:120]
        voice["access_token"] = str(voice.get("access_token") or "").strip()
        voice["secret_key"] = str(voice.get("secret_key") or "").strip()
        voice["resource_id"] = str(voice.get("resource_id") or "volc.seedasr.auc").strip()[:120] or "volc.seedasr.auc"
        for key, default, minimum, maximum in (
            ("single_duration_threshold_seconds", 20, 1, 3_600),
            ("chat_cumulative_threshold_seconds", 60, 1, 86_400),
        ):
            try:
                voice[key] = max(minimum, min(maximum, int(voice.get(key, default))))
            except (TypeError, ValueError):
                voice[key] = default
        try:
            voice["low_confidence_threshold"] = max(
                0.0, min(1.0, float(voice.get("low_confidence_threshold", 0.75)))
            )
        except (TypeError, ValueError):
            voice["low_confidence_threshold"] = 0.75
        voice["keep_source_audio"] = bool(voice.get("keep_source_audio", False))

        profile = value.setdefault("profile", {})
        for key in ("roles", "projects", "organizations", "key_contacts", "topics", "suggestions"):
            raw_values = profile.get(key) if isinstance(profile.get(key), list) else []
            profile[key] = list(dict.fromkeys(
                str(item).strip()[:120] for item in raw_values if str(item).strip()
            ))[:100]
        return value

    def snapshot(self, include_secrets: bool = False) -> Dict[str, Any]:
        with self._lock:
            value = copy.deepcopy(self._data)
        if not include_secrets:
            value.setdefault("ai", {}).pop("api_key", None)
            value.setdefault("media", {}).pop("image_aes_key", None)
            value.setdefault("voice", {}).pop("access_token", None)
            value.setdefault("voice", {}).pop("secret_key", None)
            value.setdefault("email", {}).pop("password", None)
        return value

    def public(self) -> Dict[str, Any]:
        with self._lock:
            value = copy.deepcopy(self._data)
        ai = value.setdefault("ai", {})
        api_key = str(ai.pop("api_key", "") or "")
        ai["api_key_configured"] = bool(api_key or os.environ.get("OPENAI_API_KEY"))
        ai["api_key_masked"] = _mask_secret(api_key)
        media = value.setdefault("media", {})
        image_key = str(media.pop("image_aes_key", "") or "")
        media["image_key_configured"] = bool(image_key)
        media["image_key_masked"] = _mask_secret(image_key)
        voice = value.setdefault("voice", {})
        access_token = str(voice.pop("access_token", "") or "")
        secret_key = str(voice.pop("secret_key", "") or "")
        voice["access_token_configured"] = bool(access_token)
        voice["access_token_masked"] = _mask_secret(access_token)
        voice["secret_key_configured"] = bool(secret_key)
        voice["secret_key_masked"] = _mask_secret(secret_key)
        email = value.setdefault("email", {})
        email_password = str(email.pop("password", "") or "")
        email["password_configured"] = bool(email_password)
        email["password_masked"] = _mask_secret(email_password)
        value["settings_file"] = str(self.path.resolve())
        value["constraints"] = {
            "refresh_interval_ms": {
                "min": _MIN_REFRESH_MS,
                "max": _MAX_REFRESH_MS,
            },
            "analysis_interval_ms": {"min": _MIN_ANALYSIS_MS, "max": _MAX_ANALYSIS_MS},
            "send_enabled": False,
            "ai_mode": "manual_only",
        }
        return value

    def update(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ValueError("设置必须是 JSON 对象")
        with self._lock:
            candidate = copy.deepcopy(self._data)
            for section in ("display", "refresh", "analysis", "media", "ai", "voice", "profile", "email"):
                incoming = payload.get(section)
                if isinstance(incoming, Mapping):
                    self._merge(candidate.setdefault(section, {}), incoming)
            # An empty API key means “keep the current key” in the UI.  A
            # separate clear flag is required to remove it deliberately.
            ai_payload = payload.get("ai")
            if isinstance(ai_payload, Mapping):
                if ai_payload.get("clear_api_key") is True:
                    candidate.setdefault("ai", {})["api_key"] = ""
                elif "api_key" in ai_payload and str(ai_payload.get("api_key") or "").strip():
                    candidate.setdefault("ai", {})["api_key"] = str(ai_payload.get("api_key")).strip()
                else:
                    candidate.setdefault("ai", {})["api_key"] = self._data.get("ai", {}).get("api_key", "")
                candidate.setdefault("ai", {}).pop("clear_api_key", None)
            media_payload = payload.get("media")
            if isinstance(media_payload, Mapping) and "image_aes_key" not in media_payload:
                candidate.setdefault("media", {})["image_aes_key"] = self._data.get("media", {}).get("image_aes_key", "")
            voice_payload = payload.get("voice")
            if isinstance(voice_payload, Mapping):
                for key in ("access_token", "secret_key"):
                    clear_key = "clear_%s" % key
                    if voice_payload.get(clear_key) is True:
                        candidate.setdefault("voice", {})[key] = ""
                    elif key in voice_payload and str(voice_payload.get(key) or "").strip():
                        candidate.setdefault("voice", {})[key] = str(voice_payload.get(key)).strip()
                    else:
                        candidate.setdefault("voice", {})[key] = self._data.get("voice", {}).get(key, "")
                    candidate.setdefault("voice", {}).pop(clear_key, None)
            email_payload = payload.get("email")
            if isinstance(email_payload, Mapping):
                if email_payload.get("clear_password") is True:
                    candidate.setdefault("email", {})["password"] = ""
                elif "password" in email_payload and str(email_payload.get("password") or "").strip():
                    candidate.setdefault("email", {})["password"] = str(email_payload.get("password")).strip()
                else:
                    candidate.setdefault("email", {})["password"] = self._data.get("email", {}).get("password", "")
                candidate.setdefault("email", {}).pop("clear_password", None)
            normalized = self._normalize(candidate)
            self._write(normalized)
            self._data = normalized
        return self.public()

    def _write(self, value: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(str(temporary), str(self.path))
