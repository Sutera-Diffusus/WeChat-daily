"""M0 environment and wxauto4 capability checks."""

import importlib.metadata
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple


SUPPORTED_CLIENT_MAX = "4.1.8.107"


@dataclass(frozen=True)
class WeChatProcess:
    pid: int
    name: str
    path: Optional[str]
    version: Optional[str]


@dataclass(frozen=True)
class M0Report:
    python_version: str
    platform: str
    wxauto4_version: Optional[str]
    wxauto4_import_error: Optional[str]
    listener_api: bool
    polling_api: bool
    processes: List[WeChatProcess] = field(default_factory=list)
    supported_client_max: str = SUPPORTED_CLIENT_MAX
    compatibility: str = "unknown"
    next_action: str = ""
    connection_ok: Optional[bool] = None
    connection_message: Optional[str] = None


def _version_tuple(value: str) -> Tuple[int, ...]:
    values = []
    for part in value.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        values.append(int(digits or 0))
    return tuple(values)


def _file_version(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    try:
        import win32api

        info = win32api.GetFileVersionInfo(path, "\\")
        return "%s.%s.%s.%s" % (
            win32api.HIWORD(info["FileVersionMS"]),
            win32api.LOWORD(info["FileVersionMS"]),
            win32api.HIWORD(info["FileVersionLS"]),
            win32api.LOWORD(info["FileVersionLS"]),
        )
    except Exception:
        return None


def _find_processes() -> List[WeChatProcess]:
    try:
        import psutil
    except Exception:
        return []
    processes: List[WeChatProcess] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            name = str(process.info.get("name") or "")
            if name.lower() not in {"wechat.exe", "weixin.exe"}:
                continue
            path = process.info.get("exe")
            processes.append(
                WeChatProcess(
                    pid=int(process.info["pid"]),
                    name=name,
                    path=path,
                    version=_file_version(path),
                )
            )
        except Exception:
            continue
    return processes


def collect_m0_report(connect: bool = False) -> M0Report:
    package_version: Optional[str]
    package_error: Optional[str] = None
    try:
        package_version = importlib.metadata.version("wxauto4")
    except importlib.metadata.PackageNotFoundError:
        package_version = None
        package_error = "wxauto4 未安装"
    except Exception as exc:
        package_version = None
        package_error = str(exc)

    listener_api = False
    polling_api = False
    if package_version:
        try:
            from wxauto4 import WeChat

            listener_api = all(
                hasattr(WeChat, name) for name in ("AddListenChat", "StopListening")
            )
            polling_api = all(hasattr(WeChat, name) for name in ("GetSubWindow", "GetAllMessage"))
        except Exception as exc:
            package_error = "wxauto4 导入失败: %s" % exc

    processes = _find_processes()
    compatibility = "unknown"
    next_action = ""
    if not package_version:
        compatibility = "blocked_missing_wxauto4"
        next_action = "安装项目锁定的 wxauto4==41.1.2"
    elif not processes:
        compatibility = "needs_running_wechat"
        next_action = "启动并登录微信，再运行 python -m wechat_bridge m0-check --connect"
    else:
        versions = [p.version for p in processes if p.version]
        if not versions:
            compatibility = "manual_version_check_required"
            next_action = "记录微信 exe 完整文件版本，并手动执行 M0 兼容性测试"
        elif all(_version_tuple(v) <= _version_tuple(SUPPORTED_CLIENT_MAX) for v in versions):
            compatibility = "candidate_supported"
            next_action = "执行文件传输助手收发测试，并观察遮挡/最小化/焦点行为"
        else:
            compatibility = "above_documented_max"
            compatibility = "above_wxauto4_max_db_route_available"
            next_action = (
                "当前 4.x 使用 wechatauto_db；不要把 wxauto4 或未匹配的 Hook "
                "当作底座"
            )

    connection_ok: Optional[bool] = None
    connection_message: Optional[str] = None
    if connect:
        try:
            from .adapters.wxauto4 import WxAuto4Adapter

            adapter = WxAuto4Adapter()
            adapter.connect()
            health = adapter.health_check()
            connection_ok = health.ok
            connection_message = health.message
            adapter.disconnect()
        except Exception as exc:
            connection_ok = False
            connection_message = str(exc)

    return M0Report(
        python_version=sys.version.split()[0],
        platform="%s %s" % (platform.system(), platform.release()),
        wxauto4_version=package_version,
        wxauto4_import_error=package_error,
        listener_api=listener_api,
        polling_api=polling_api,
        processes=processes,
        compatibility=compatibility,
        next_action=next_action,
        connection_ok=connection_ok,
        connection_message=connection_message,
    )


def report_dict(report: M0Report) -> Dict[str, Any]:
    return asdict(report)


def format_report(report: M0Report, as_json: bool = False) -> str:
    data = report_dict(report)
    if as_json:
        return json.dumps(data, ensure_ascii=False, indent=2)
    lines = [
        "M0 环境检查",
        "- Python: %s" % report.python_version,
        "- 平台: %s" % report.platform,
        "- wxauto4: %s" % (report.wxauto4_version or "未安装"),
        "- 回调监听 API: %s" % ("可用" if report.listener_api else "不可用"),
        "- 轮询 API: %s" % ("可用" if report.polling_api else "不可用"),
        "- 微信进程: %s" % ("运行中" if report.processes else "未发现"),
    ]
    for process in report.processes:
        lines.append(
            "  - pid=%s name=%s version=%s path=%s"
            % (process.pid, process.name, process.version or "未知", process.path or "未知")
        )
    lines.extend(
        [
            "- 兼容性结论: %s" % report.compatibility,
            "- 下一步: %s" % report.next_action,
        ]
    )
    if report.connection_ok is not None:
        lines.append(
            "- 只读连接检查: %s (%s)"
            % ("通过" if report.connection_ok else "失败", report.connection_message or "无详情")
        )
    if report.wxauto4_import_error:
        lines.append("- 诊断信息: %s" % report.wxauto4_import_error)
    return "\n".join(lines)
