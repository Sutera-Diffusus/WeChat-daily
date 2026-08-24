"""Tiny stdio MCP facade for the local WeChat Bridge HTTP API.

It intentionally uses only the Python standard library so the Codex plugin
can start without adding another runtime dependency. Start the bridge with
``wechat_bridge run --dashboard`` before invoking these tools.
"""

import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_URL = os.environ.get("WECHAT_BRIDGE_URL", "http://127.0.0.1:8765").rstrip("/")


TOOLS = [
    {
        "name": "wechat.status",
        "description": "读取本地微信桥接服务、适配器、白名单和任务计数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "wechat.recent_messages",
        "description": "读取最近消息；默认只用于文件传输助手测试范围。",
        "inputSchema": {
            "type": "object",
            "properties": {"chat_id": {"type": "string"}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}},
            "additionalProperties": False,
        },
    },
    {
        "name": "wechat.enable_auto_reply",
        "description": "恢复自动回复；不会解除文件传输助手安全闸门。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "wechat.disable_auto_reply",
        "description": "暂停自动回复，但继续接收和记录消息。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "wechat.reply_preview",
        "description": "生成一条不会入队、不会发送的回复预览。",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}, "chat_id": {"type": "string"}, "chat_name": {"type": "string"}},
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wechat.retry_message",
        "description": "重新入队一条已耗尽重试次数的失败任务。",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"], "additionalProperties": False},
    },
    {
        "name": "wechat.send_text",
        "description": "受控人工发送接口；必须显式确认、关闭演练模式且目标为允许范围。",
        "inputSchema": {
            "type": "object",
            "properties": {"content": {"type": "string"}, "chat_id": {"type": "string"}, "chat_name": {"type": "string"}, "confirm": {"type": "boolean"}},
            "required": ["content", "confirm"],
            "additionalProperties": False,
        },
    },
]


def api(path, method="GET", payload=None, query=None):
    url = BASE_URL + path
    if query:
        url += "?" + urlencode(query)
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=8) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            body = {"error": str(exc)}
        return exc.code, body
    except URLError as exc:
        return 503, {"error": "bridge_unreachable", "message": str(exc.reason)}


def call_tool(name, arguments):
    arguments = arguments or {}
    if name == "wechat.status":
        return api("/api/status")
    if name == "wechat.recent_messages":
        query = {"limit": int(arguments.get("limit", 20))}
        if arguments.get("chat_id"):
            query["chat"] = arguments["chat_id"]
        return api("/api/messages", query=query)
    if name == "wechat.enable_auto_reply":
        return api("/api/auto-reply", method="POST", payload={"enabled": True})
    if name == "wechat.disable_auto_reply":
        return api("/api/auto-reply", method="POST", payload={"enabled": False})
    if name == "wechat.reply_preview":
        return api("/api/preview", method="POST", payload=arguments)
    if name == "wechat.retry_message":
        return api("/api/retry", method="POST", payload=arguments)
    if name == "wechat.send_text":
        return api("/api/send-text", method="POST", payload=arguments)
    return 404, {"error": "unknown_tool", "message": name}


def send(message):
    if message.get("method") == "notifications/initialized":
        return None
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-03-26", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "wechat-bridge", "version": "0.1.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = message.get("params") or {}
        status, value = call_tool(params.get("name"), params.get("arguments"))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"isError": status >= 400, "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}]}}
    if request_id is not None:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
    return None


for line in sys.stdin:
    try:
        message = json.loads(line)
        response = send(message)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    except Exception as exc:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(exc)}}, ensure_ascii=False) + "\n")
        sys.stdout.flush()
