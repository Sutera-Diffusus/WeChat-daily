# 微日报桌面端（Tauri 2）

桌面端只封装现有本地只读服务，不新增微信发送能力。启动时会先检查 `127.0.0.1:8765/api/status`：

- 已有微日报服务：直接复用，退出桌面端时不会关闭它；
- 没有服务：开发态启动项目 `.venv` 中的 Python；安装包启动 PyInstaller sidecar；退出时只终止自己启动的进程。

## 开发运行

Windows 需要 Rust stable-msvc、Microsoft C++ Build Tools 和 WebView2。安装项目 Python 依赖后：

```powershell
cd desktop
npm install
npm run icons
npm run dev
```

可通过 `WEI_DAILY_PROJECT_ROOT` 指定 Python 项目根目录；默认按 `desktop/src-tauri/../..` 解析。

## 构建 Windows 安装包

```powershell
cd desktop
npm install
npm run build
```

构建流程先生成品牌图标，再使用 PyInstaller 生成带目标三元组后缀的 `wei-daily-backend`，最后由 Tauri 2 生成 NSIS 安装包。生产数据库位于系统应用数据目录，不写入安装目录。

## 安全边界

- 后端命令不包含 `--live`，默认 dry-run；
- 桌面等待页只调用 `backend_ready` 和 `open_dashboard`；
- 前端未获得 shell 执行权限；
- 仅绑定 `127.0.0.1:8765`。

## 普通用户使用发布包

- 安装包：双击 `微日报_0.1.4_x64-setup.exe`，安装后从开始菜单或桌面快捷方式启动“微日报”；
- 便携包：完整解压 `微日报-便携版-0.1.4-win-x64.zip`，只双击其中的 `wei-daily-desktop.exe`；
- 便携包中的 `wei-daily-backend.exe` 必须和主程序同目录，不能单独启动或拆出来；
- `127.0.0.1:8765` 是桌面主程序与内置后端之间的本机回环通道，用户不需要手动启动端口、Python 服务或浏览器页面。

便携包内还包含 `使用说明.md`、`SHA256SUMS.txt` 和 `version.txt`。如果提示端口被占用，请先关闭其它微日报实例或旧的本地服务。
