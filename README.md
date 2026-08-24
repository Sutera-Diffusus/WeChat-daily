# 微日报

**微日报 0.1.4**——一个面向 Windows 的本地优先微信收发、历史档案与情报分析工作台。

它把本地微信消息整理成可追溯的日报素材：

`微信 → 适配器 → 统一消息 → SQLite → 日/周历史档案 → 可解释分析 → 微日报工作台`

当前版本默认是**只接收、只读分析**：历史导入不会创建回复任务，发送接口保留为预览/测试能力并由运行时硬闸门保护。

[![Version](https://img.shields.io/badge/version-0.1.4-4da3ff)](CHANGELOG.md)
[![Python](https://img.shields.io/badge/Python-3.9--3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tauri](https://img.shields.io/badge/Tauri-2-24C8DB?logo=tauri&logoColor=white)](desktop/)

---

## 目录

- [特性](#特性)
- [安装要求](#安装要求)
- [快速开始](#快速开始)
- [微日报工作台](#微日报工作台)
- [桌面端](#桌面端)
- [规则与 AI](#规则与-ai)
- [API 与 Codex 插件](#api-与-codex-插件)
- [数据与隐私](#数据与隐私)
- [项目结构](#项目结构)
- [开发与测试](#开发与测试)
- [当前边界](#当前边界)
- [License](#license)

## 特性

### 📥 本地消息接收

- `wechatauto_db`：读取当前微信 4.x 本地加密数据库的可读消息分片；
- `wxauto4`：保留为显式回退适配器；
- Hook HTTP：保留 `QueryDB/status`、`SendTextMsg` 和 `D0003` 回调适配层；
- 统一消息模型覆盖聊天、发送者、消息类型、群聊标记、媒体键和证据 ID；
- 消息去重、任务状态、重试、发送尝试、错误与确认结果统一落入本地 SQLite。

### 🗂️ 日 / 周历史档案

- 支持今天、近 7 天和自定义日期范围；
- 跨所有可读会话与加密消息分片读取历史，不因重复运行产生重复消息；
- 按 `Asia/Shanghai` 解释日期边界，内部使用半开区间；
- 记录同步范围、进度和最近一次运行状态，便于排查缺口。

### 🧭 可解释情报分析

- 消息量、活跃会话、小时分布和主题概览；
- 重点线索、行动候选、事件时间窗和风险提示；
- 分析结论保留原消息证据 ID，可回链到具体消息；
- 规则支持关键词、正则、聊天/发送者、消息类型、时间段和时区。

### 🖥️ 本地控制台与桌面封装

- 浏览器工作台绑定 `127.0.0.1:8765`，展示档案、摘要、线索和行动候选；
- 消息档案支持筛选、搜索和证据回链；
- Tauri 2 桌面端复用已有本地服务，服务未运行时可启动自己的后端进程；
- 桌面端不开放前端 shell 权限，不传入 `--live`，退出时只关闭自己启动的后端。

### 🧩 Codex 本地插件

`plugins/wechat-bridge` 提供本地 MCP 工具，调用同一套 loopback API，适合在 Codex 中查询状态、消息和分析结果。插件不绕过后端的只读边界。

## 安装要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 |
| Python | 3.9–3.12 |
| 微信 | 已登录，并保持主窗口打开 |
| Node.js | 18+（仅开发或构建桌面端需要） |
| Rust | stable-msvc（仅构建 Tauri 安装包需要） |
| WebView2 | Windows 桌面端运行时需要 |

当前开发环境以微信 `4.1.12.26` 为主。微信版本、数据库布局和 Hook 行为变化时，适配器可能需要重新验证。

## 快速开始

### 1. 创建 Python 环境

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

固定规则模式不需要 API Key。项目依赖包含 Windows 时区数据，Windows 上不要求额外安装系统时区数据库。

### 2. 启动本地工作台

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge run `
  --adapter wechatauto_db `
  --chat "文件传输助手" `
  --dashboard
```

然后打开 <http://127.0.0.1:8765>。

工作台提供：

- 今天 / 近 7 天 / 自定义日期窗口；
- “抓取当前范围”历史同步；
- 消息总量、会话、重点线索和行动候选；
- 消息档案搜索、筛选和证据回链；
- 规则预览、同步状态和运行边界提示。

### 3. 进行一次只读检查

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge m0-check
.\.venv\Scripts\python.exe -m wechat_bridge hook-check --base-url http://127.0.0.1:30001
```

`m0-check` 只检查 Python、依赖和微信连接条件；`hook-check` 只检查本地 Hook HTTP 服务，不下载 DLL、不注入微信，也不替换微信目录文件。

## 微日报工作台

### 工作流

1. 启动微信并确认登录状态；
2. 启动微日报服务；
3. 在工作台选择日期范围并执行“抓取当前范围”；
4. 先查看同步状态，再阅读重点线索和行动候选；
5. 通过证据 ID 回到消息档案核对原文；
6. 需要长期保留的结论在工作台外另行整理，数据库仍只作为本地运行数据。

### 服务接口

```text
GET  /api/status
GET  /api/messages?limit=50
GET  /api/messages?start=YYYY-MM-DD&end=YYYY-MM-DD&limit=50000
GET  /api/tasks?limit=50
GET  /api/rules
GET  /api/accounts
GET  /api/insights?start=YYYY-MM-DD&end=YYYY-MM-DD
GET  /api/chats?start=YYYY-MM-DD&end=YYYY-MM-DD
GET  /api/sync-status
GET  /api/ai-status
POST /api/auto-reply       {"enabled": false}
POST /api/preview          {"content": "..."}
POST /api/sync             {"limit": 100}
POST /api/sync-range       {"start":"YYYY-MM-DD","end":"YYYY-MM-DD","scope":"all"}
POST /api/ai-analysis      {"start":"YYYY-MM-DD","end":"YYYY-MM-DD","limit":120,"confirm":true}
POST /api/retry            {"task_id": 1}
POST /api/send-text        {"content": "...", "confirm": true}
```

服务只绑定 `127.0.0.1`，不会返回 API Key。当前只读模式下，`/api/send-text` 固定返回 403；`/api/preview` 不创建任务，历史同步不会创建回复任务。

## 桌面端

桌面封装位于 [`desktop/`](desktop)，使用 Tauri 2。它沿用已有本地服务：若 `127.0.0.1:8765/api/status` 已可用，则直接复用；否则开发态启动项目虚拟环境中的 Python，生产态启动 PyInstaller sidecar。

### 开发运行

```powershell
cd desktop
npm install
npm run icons
npm run dev
```

可通过 `WEI_DAILY_PROJECT_ROOT` 指定 Python 项目根目录；默认解析为桌面端目录的上两级。

### 构建 Windows 安装包

```powershell
cd desktop
npm install
npm run build
```

构建流程会生成品牌图标、构建 `wei-daily-backend` sidecar，再生成 NSIS 安装包。构建产物、sidecar 和 Tauri target 均不会提交到仓库。

## 规则与 AI

### 规则配置

复制示例后按需修改：

```powershell
Copy-Item config\rules.example.json config\rules.json
```

启动时指定规则文件：

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge run `
  --rules-file config\rules.json `
  --chat "文件传输助手" `
  --dashboard
```

规则按文件顺序匹配，命中第一条后停止。布尔字段必须使用 JSON `true/false`，字符串 `"false"` 会被拒绝。

### AI 辅助分析

AI 是手动二次筛选，不是自动回复。使用前设置：

```powershell
$env:OPENAI_API_KEY = "你的 API Key"
```

然后在工作台中手动确认 AI 分析。服务会把有限字段和证据编号交给配置的 AI 服务，结果必须回链到本地证据 ID；无法回链的结论会被丢弃。分析结果不写入消息库、不入回复队列，也不会自动发送微信。

## API 与 Codex 插件

插件目录为 [`plugins/wechat-bridge`](plugins/wechat-bridge)。它提供：

- `wechat.status`
- `wechat.recent_messages`
- `wechat.enable_auto_reply`
- `wechat.disable_auto_reply`
- `wechat.reply_preview`
- `wechat.retry_message`
- `wechat.send_text`

插件调用仍受服务端 dry-run、健康检查和目标闸门约束；工具名称不代表当前版本已经开放自动发送。

## 数据与隐私

- `data/` 保存本地数据库、同步状态和 QA 浏览器 profile，已整体加入 `.gitignore`；
- `tmp/`、`output/`、`wechatauto_logs/` 保存运行日志、截图、PDF 和安装包等本地产物，不提交；
- 仓库不包含微信数据库、聊天记录、Cookie、浏览器登录数据、API Key 或个人配置；
- AI 分析只有在用户手动确认后才会把有限候选文本发送到配置的 AI 服务；
- 普通接收、历史同步、规则分析和桌面端均只访问本机服务；
- 发现安全问题请参阅 [`SECURITY.md`](SECURITY.md)。

## 项目结构

```text
wei-daily/
├─ src/wechat_bridge/           # Python 服务、适配器、存储、分析与 Web 工作台
│  ├─ adapters/                 # wechatauto_db / wxauto4 / Hook HTTP
│  └─ web/                      # 本地控制台前端与品牌资源
├─ desktop/                     # Tauri 2 桌面端与 PyInstaller sidecar 构建脚本
│  ├─ src/                      # 桌面等待页与本地服务启动逻辑
│  └─ src-tauri/                # Rust 容器、权限和安装包配置
├─ plugins/wechat-bridge/      # Codex 本地 MCP 插件
├─ tests/                       # 规则、同步、适配器、分析和 API 测试
├─ docs/                        # 设计评审与实现资料
├─ config/rules.example.json   # 可复制的规则配置示例
├─ pyproject.toml
├─ CHANGELOG.md
├─ CONTRIBUTING.md
├─ SECURITY.md
└─ LICENSE
```

## 开发与测试

安装开发依赖并运行测试：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

测试覆盖规则和时区、AI 缺失配置、消息去重、任务恢复、跨分片数据库适配器、历史同步、可解释分析、只读发送闸门、控制台 API、媒体和语音流水线等。

提交前请确认：

- `git status` 中没有 `data/`、`tmp/`、`output/`、浏览器 profile 或日志；
- 没有 `.env`、私钥、Cookie、数据库或安装包；
- 公开到其他环境的日志和截图已经脱敏；
- 相关测试和 `pip check` 已通过。

更完整的修改约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 当前边界

- 当前版本的主运行路径是接收与分析，不是无人值守自动回复；
- 微信 `4.1.12.26` 的 Hook DLL 匹配尚未在本项目中验证，不能把公开的其他版本 Hook 目标视为兼容；
- 项目不会下载 DLL、注入微信或替换微信安装目录文件；
- `wxauto4` 作为回退适配器保留，实际可用性取决于本机微信窗口和依赖版本；
- 微信数据库、媒体和语音格式属于第三方应用内部实现，升级微信后需要重新运行诊断和测试。

## License

[MIT](LICENSE)

---

**把零散消息整理成今天真正有用的几行。** 🗞️
