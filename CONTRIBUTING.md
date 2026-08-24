# Contributing

感谢你关注微日报。这个项目会接触本地微信消息，因此提交内容除了可运行，也必须能够被安全地复现和审阅。

## 开发环境

- Windows 10 / 11；
- Python 3.9–3.12；
- 微信已登录并保持主窗口打开；
- Node.js 18+（桌面端开发）；
- Rust stable-msvc、Microsoft C++ Build Tools 和 WebView2（桌面端构建）。

初始化环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

## 修改与验证

1. 先确认改动属于 `src/`、`desktop/src/`、`desktop/src-tauri/`、`plugins/`、`tests/` 或文档范围；
2. 涉及同步、数据库、规则或发送边界的改动，优先补充回归测试；
3. 运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

4. 如果改动桌面端，再运行 `cd desktop; npm run dev` 做人工检查；
5. 提交前确认没有把本地数据库、浏览器 profile、日志、截图、安装包或凭据带入 Git。

## 数据与凭据

- `data/`、`tmp/`、`output/` 和 `wechatauto_logs/` 只用于本地运行或 QA；
- 不提交聊天记录、微信数据库、Cookie、浏览器登录状态、备份目录或原始媒体；
- `OPENAI_API_KEY` 只通过环境变量提供，永远不要写入源码、规则文件或测试快照；
- 需要展示 UI 时使用脱敏数据，避免直接上传运行时截图；
- 新增外部依赖时说明用途、版本和对本地隐私边界的影响。

## 提交规范

建议使用清晰的 Conventional Commit 前缀：

- `feat:` 新功能；
- `fix:` 缺陷修复；
- `test:` 测试；
- `docs:` 文档；
- `chore:` 构建、依赖或工具链。

一次提交尽量只处理一件事，并在提交说明中写清验证方式。

## 运行边界

微日报当前默认只接收、只读分析。涉及自动回复、Hook、数据库解密或外部 AI 调用的改动，必须明确说明：

- 是否会创建回复任务；
- 是否会读取或发送哪些数据；
- 是否需要用户手动确认；
- 是否保持 `127.0.0.1` 绑定和发送硬闸门。

更多安全要求见 [`SECURITY.md`](SECURITY.md)。
