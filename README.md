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
- [产品预览](#产品预览)
- [功能详解](#功能详解)
- [安装要求](#安装要求)
- [快速开始](#快速开始)
- [新手教程](#新手教程)
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

## 产品预览

下面是微日报本地工作台的实际界面截图。截图中的聊天名称、联系人和部分正文已经模糊处理；图片只用于展示产品布局，不代表仓库会提交任何运行时聊天数据。

| 页面 | 说明 |
| --- | --- |
| <img src="docs/images/product-preview/overview.png" alt="微日报总览与日报主线" width="560"> | **总览 / 日报主线**：选择日期范围后查看消息量、会话数、重点候选、待处理项和语音转写进度，并阅读“今天发生了什么”。[查看原图](docs/images/product-preview/overview.png) |
| <img src="docs/images/product-preview/daily-edition.png" alt="微日报日报内容页" width="560"> | **日报内容页**：把高信号消息排成可阅读的版面，保留主题、判断、标签和证据附录。[查看原图](docs/images/product-preview/daily-edition.png) |
| <img src="docs/images/product-preview/small-things.png" alt="微日报小事列表" width="560"> | **小事**：保留低信号但可能有后续价值的日常消息，既不把它们混进主线，也不让它们悄悄消失。[查看原图](docs/images/product-preview/small-things.png) |
| <img src="docs/images/product-preview/conversations.png" alt="微日报会话浏览" width="560"> | **会话**：按聊天查看原始消息、图片/文件/语音类型、媒体路径和会话统计，支持回到原始证据。[查看原图](docs/images/product-preview/conversations.png) |
| <img src="docs/images/product-preview/workbench.png" alt="微日报工作台与分析" width="560"> | **工作台 / 分析**：集中查看待处理候选、事件主线、主题脉络、当前判断和分析指标；默认只读，不会因为出现候选就自动发送消息。[查看原图](docs/images/product-preview/workbench.png) |

## 功能详解

微日报不是一个单纯的聊天记录查看器，而是一条“采集—归档—筛选—解释—复核”的本地工作流。它把消息放进有日期、有来源、有证据的上下文里，让用户先看到值得注意的内容，再随时回到原始消息核对。

### 1. 消息采集：把不同入口统一成一套消息

- `wechatauto_db` 负责读取当前微信 4.x 本地数据库中的可读消息分片；
- `wxauto4` 作为显式回退适配器保留，适合需要通过微信窗口获取消息的环境；
- Hook HTTP 适配层保留 `QueryDB/status`、`SendTextMsg` 和 `D0003` 回调接口，便于接入已有本地桥接服务；
- 不同来源最终都会转换成统一消息模型，包含聊天、发送者、时间、消息类型、群聊标记、媒体键和证据 ID；
- 消息去重、同步进度、任务状态、重试记录、错误和确认结果都写入本地 SQLite，避免同一批历史消息重复出现。

项目的安全边界也很明确：主路径不下载 DLL、不注入微信、不替换微信安装目录文件，接收和分析默认只访问本机数据与 loopback 服务。

### 2. 历史归档：按天、按周、按范围复盘

工作台支持“今天”“近 7 天”和自定义日期范围。日期按照 `Asia/Shanghai` 解释，内部使用半开区间，因此跨午夜和跨天同步时不容易出现边界重复或漏读。

每次同步都会记录范围、进度和最近一次运行状态。适配器会跨可读会话与数据库分片查找历史消息，并通过稳定的消息标识去重。这样既可以每天只抓当天，也可以在第一次使用时补齐近一周的上下文。

### 3. 日报编排：从“消息很多”变成“今天发生了什么”

日报页面会把消息按信号强弱组织成几层内容：

- **主线**：当天最值得先读的主题、变化和事件；
- **重点候选**：可能需要关注、跟进或进一步判断的消息；
- **事件主线**：把同一件事在不同时间、不同会话里的消息串起来；
- **主题脉络**：按关键词、聊天、发送者和消息类型观察讨论集中在哪里；
- **小事**：保留低信号但可能有后续价值的日常消息；
- **证据附录**：为每一条判断保留原始消息证据 ID，支持回到档案核对。

日报不是把模型输出直接当成结论。固定规则先进行本地筛选和打标，分析层计算消息量、活跃会话、小时分布、主题和候选项；如用户主动开启 AI，AI 只做二次整理，且必须引用本地证据编号，无法回链的结论会被丢弃。

### 4. 会话浏览：随时回到原始消息

会话页提供从摘要回到证据的路径：左侧按聊天查找，中间查看消息正文和图片、文件、语音等类型，右侧查看会话统计与相关状态。媒体字段保留本地媒体键或路径，方便在本机进一步核对。

这一步很重要：日报里的“重点”只是帮助排序，用户可以在会话页确认上下文、前后消息和原始发送者，再决定是否把它当成真正的事项。会话浏览本身不会发送消息。

### 5. 工作台：把候选变成可处理事项

工作台把待处理候选、事件主线、主题脉络和分析指标放在同一个视图中。每个候选都应带有来源、时间、规则或分析原因以及证据链接，便于判断“为什么会出现”和“下一步是否需要处理”。

“待处理”“已确认”等状态只代表本地工作台里的整理状态，不等于已经向微信联系人发送了内容。当前版本的 `/api/send-text` 固定返回 403；预览、重试和分析接口也受到服务端健康检查与只读闸门约束。

### 6. 规则与 AI：先本地筛选，再选择性调用

规则文件支持关键词、正则表达式、聊天、发送者、消息类型、时间段和时区。推荐的处理顺序是：

1. 先用规则缩小消息范围并打标签；
2. 再按日期、会话和消息类型查看候选；
3. 回到原始证据确认上下文；
4. 只有确实需要时，手动确认 AI 二次分析；
5. 检查 AI 结果是否能回链到本地证据。

这样可以把隐私暴露和分析成本控制在最小范围。AI 不参与自动回复，不写入消息库，不进入回复队列，也不会因为分析完成而自动触发微信动作。

### 7. 桌面端与 Codex 插件

Tauri 2 桌面端复用同一套本地服务：已有服务运行时直接连接 `127.0.0.1:8765`，未运行时再启动自己的后端进程。桌面端不开放前端 shell 权限，不传入 `--live`，退出时只关闭自己启动的进程。

`plugins/wechat-bridge` 则把状态、近期消息、规则分析和预览能力暴露给 Codex。它调用的仍然是同一套本地 loopback API，因此权限边界、只读模式和发送硬闸门不会因为换了入口而改变。

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

## 新手教程

第一次使用时，建议先完成“本地接收 + 只读日报”这条最小路径，确认同步和证据回链正常后，再配置规则或 AI。下面的命令以当前项目目录 `D:\Project_Codex\Project_WeChatMoreFunction` 为例。

### 第 0 步：确认使用边界

开始前请确认：

- Windows 10 / 11 已登录微信，并保持微信主窗口打开；
- 已安装 Python 3.9–3.12，推荐 Python 3.12；
- 只把需要分析的本地数据交给微日报，仓库本身不包含聊天数据库、Cookie 或 API Key；
- 第一次运行先保持默认只读模式，不要把 `--live` 或自动回复能力加入启动命令。

### 第 1 步：安装项目

打开 PowerShell，进入项目目录并创建虚拟环境：

```powershell
cd D:\Project_Codex\Project_WeChatMoreFunction
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m pip check
```

看到 `No broken requirements found.` 就表示依赖关系正常。项目移动到新磁盘后，如果出现 `No module named wechat_bridge`，重新执行一次 `pip install -e .` 即可修复 editable 安装指向旧路径的问题。

### 第 2 步：启动本地工作台

在同一个 PowerShell 窗口执行：

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge run `
  --adapter wechatauto_db `
  --chat "文件传输助手" `
  --dashboard
```

然后用浏览器打开 <http://127.0.0.1:8765>。`--chat` 是示例目标；如果要读取所有可读会话，可按本机适配器支持情况省略它，或在工作台里选择全部范围。

如果页面打不开，先不要重复启动多个服务，按以下顺序检查：

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge m0-check
Get-NetTCPConnection -LocalPort 8765 -ErrorAction SilentlyContinue
```

### 第 3 步：完成第一次同步

1. 在工作台选择“今天”，先执行一次“抓取当前范围”；
2. 等待同步状态变为完成，再查看消息总量和会话数；
3. 回到“日报主线”，确认是否出现主题、重点候选和证据编号；
4. 确认当天流程正常后，再切换到“近 7 天”或自定义日期范围补齐历史。

第一次同步可能需要更久，因为适配器要扫描可读数据库分片并建立本地索引。同步过程中不要同时启动第二个 `wechat_bridge run`，也不要手动移动 `data/` 目录。

### 第 4 步：按正确顺序阅读

推荐的阅读顺序是：

1. **总览**：先看消息量、会话数、候选数和同步状态，判断数据是否完整；
2. **日报主线**：阅读“今天发生了什么”，快速掌握高信号变化；
3. **证据附录**：点击证据 ID，确认摘要是否准确；
4. **会话**：查看前后文、发送者以及图片、文件、语音等消息类型；
5. **工作台 / 分析**：整理需要跟进的候选，查看事件和主题的聚合结果；
6. **小事**：最后浏览低信号消息，避免遗漏潜在后续。

重点候选不是最终结论，分析指标也不是自动决策。真正需要处理的事项，应先回到原始消息确认上下文。

### 第 5 步：添加自己的规则

先复制示例文件：

```powershell
Copy-Item config\rules.example.json config\rules.json
```

然后编辑 `config\rules.json`，例如只关注某个聊天或包含某组关键词的消息。修改后重启服务并指定规则文件：

```powershell
.\.venv\Scripts\python.exe -m wechat_bridge run `
  --rules-file config\rules.json `
  --chat "文件传输助手" `
  --dashboard
```

规则按文件顺序匹配，命中第一条后停止；布尔值要写成 JSON 的 `true` / `false`，不要写成字符串 `"true"` / `"false"`。建议先用少量规则验证结果，再逐步增加条件。

### 第 6 步：需要时再开启 AI 分析

固定规则已经可以完成本地日报，不配置 AI 也能使用。确实需要二次归纳时，在当前 PowerShell 会话中设置 API Key：

```powershell
$env:OPENAI_API_KEY = "你的 API Key"
```

接着在工作台中手动确认 AI 分析，并检查结果是否带有可回链的本地证据 ID。不要把 API Key 写进 README、规则文件、代码或 Git 提交中；如果使用 `.env`，也不要提交这个文件。AI 分析不会自动发送微信消息。

### 第 7 步：停止服务

回到运行服务的 PowerShell 窗口按 `Ctrl+C`。停止后，数据库和日志仍留在本机的 `data/`、`tmp/` 或 `wechatauto_logs/` 中，这些目录已经被 `.gitignore` 排除，不会随着普通 Git 提交上传。

### 常见问题

| 现象 | 处理方式 |
| --- | --- |
| `No module named wechat_bridge` | 在项目根目录执行 `python -m pip install -e .`，再重试。 |
| 浏览器提示无法连接 | 确认服务已启动，并检查 `127.0.0.1:8765` 是否被其他进程占用。 |
| 同步完成但消息为空 | 确认微信已登录、主窗口保持打开，并运行 `m0-check`；再检查日期范围和适配器选择。 |
| 候选太多 | 先缩小日期范围，再用聊天、发送者、消息类型或规则条件过滤。 |
| 发送接口返回 403 | 这是当前版本的预期只读保护，不是安装失败。 |
| AI 按钮不可用 | 检查当前会话是否设置 `OPENAI_API_KEY`，并确认只在工作台手动触发。 |

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
├─ docs/                        # 设计评审、实现资料与产品预览图
│  └─ images/product-preview/   # README 中的界面截图
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
