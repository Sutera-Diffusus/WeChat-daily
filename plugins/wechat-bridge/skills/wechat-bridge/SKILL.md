---
name: wechat-bridge
description: Use the local WeChat Bridge control API for status, recent messages, safe pause/resume, reply previews, retries, and explicitly confirmed text sends. Default scope is the File Transfer Assistant only.
---

# WeChat Bridge local interface

Use the MCP tools exposed by this plugin when the user asks about the local
WeChat bridge. The bridge must be running with its dashboard/API enabled:

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge run --chat "文件传输助手" --dashboard
```

The local API is bound to `127.0.0.1`. Treat the following rules as hard
constraints:

- `wechat.status` is read-only and should be checked before any action.
- `wechat.recent_messages` reads persisted messages; do not claim it saw a
  message unless the tool returns it.
- `wechat.reply_preview` never sends and is preferred for testing rules or AI.
- `wechat.disable_auto_reply` pauses future automatic sends but does not undo a
  message already being sent.
- `wechat.send_text` is an external side effect. Require explicit user intent,
  `confirm=true`, a healthy adapter, live mode, and the File Transfer Assistant
  scope. Never infer confirmation from a request to “check” or “preview”.
- `wechat.retry_message` is only for a task the user explicitly identifies;
  explain that an uncertain prior send may be duplicated before retrying.

Do not expose API keys or raw provider credentials. When AI generation fails,
report the concrete local error and state that no reply was sent.
