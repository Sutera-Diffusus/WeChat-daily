# K16：紧凑 Stage-A 协议离线审计

审计脚本：`D:\Project_Codex\Project_WeChatMoreFunction\tests\audit_compact_stage_a_protocol_k16.py`

## 结论

K16 的结论是 `pass_offline_protocol_only`：紧凑协议的本地输入、输出、分区、scope 和隐私投影门通过了合成审计。

这不等于语义质量通过，也不等于可以接入生产。K14 的真实 development 结果仍然是严格 JSON 不完整，且曾发生 5 次未授权重复调用；因此本审计只建议下一步最多做 1 次 synthetic health-only 协议检查，明确不放行 development、Stage B、Stage C。

## 审计范围

只读取并执行了：

- `src/wechat_bridge/compact_stage_a_protocol.py` 的纯本地 API；
- 合成的 14 条消息、20 个候选的最坏形状；
- K14 已落盘的 body-free `audit_summary.private.json` 中的标量指标。

本轮 `provider_calls=0`、`development_input_read=false`、`frozen_read=false`、`private_request_body_read=false`。合成消息正文只在内存中用于构造最大长度输入，未进入审计结果或 ledger。

## 实测协议尺寸

| 项目 | 实测值 | 上限 | 结果 |
|---|---:|---:|---|
| system 字符数 | 177 | — | 通过 |
| canonical system+user token proxy | 1440 | 1600 | 通过 |
| 完整 HTTP `messages` token proxy | 1592 | 1600 | 通过 |
| 消息行数 | 14 | 14 | 通过 |
| 候选行数 | 20 | 20 | 通过 |
| 最大合法 Stage-A 响应 token proxy | 135 | 400 | 通过 |

这里的 token proxy 是协议明确的“字符数除以 4 向上取整”保守估计；它不是 provider 返回的真实 token 计费值。完整 HTTP envelope 也被重算，而不是只统计用户 JSON。

请求和最大响应只记录了不可逆的 SHA-256：

- request hash：`eef16a3b21797014d11e2c30feb69592149ed74abe9074b543745e46045ccc1c`
- response hash：`44b13a157bfc2fafd593e014218b493aa8baf94d369a736f7c7c95f6871606a6`

## 语义边界与安全门

合成验证全部通过：

- 12 条 primary 消息恰好分配一次；context 消息只能进入 context 且不能重复；
- `unknown` 可以保留；问候 context 与话题转折可以在两个 topic 中同时表示；
- 无 reply 的第 20 个候选仍保留在请求表，不能因没有显式 reply 被丢弃；
- 伪造 alias、重复 primary、缺失 primary、重复 context、跨 scope 输出、跨 scope 请求和 Stage-B 字段均被拒绝；
- speaker/scope/kind 仍由本地权威表提供，不是模型输出；
- ledger 只含 opaque handles、计数、分区和尺寸，合成正文标记未持久化。

关键拒绝码已被独立脚本记录为：`output_primary_item`、`output_primary_duplicate`、`primary_coverage`、`duplicate_context_handle`、`output_context_item`、`output_topic_keys`、`cross_scope_handle`。

## 与 K14 的可证据支持对照

K14 body-free 审计摘要记录了：授权 5 次、合计 provider calls 10 次；provider input tokens 观察范围 2674–6016；10 行均到达 output=400、`finish=length`、strict incomplete。K14 摘要把 `OUTPUT_400_LIMIT_REACHED`、`STRICT_JSON_INCOMPLETE_AT_LENGTH` 和 `INPUT_OR_PROMPT_SCHEMA_SIZE_OR_DUPLICATION_PRESSURE` 标为推断代码，而不是已证明的 provider 传输因果。

在一个完全合成的旧式“重复材料+冗长 schema”见证包中，完整 HTTP token proxy 从 4233 降到 1592，减少 2641。这个对照只证明紧凑表和短响应确实能减小本地构造的请求；它不能证明 K14 的失败一定由交接、网络传输或 DeepSeek 误解造成，也不能代替真实 provider 验证。

## 下一步放行范围

仅建议以下一次性动作：

- 最多 1 次 `synthetic_health_only`；
- 模型 `deepseek-v4-flash`；
- `response_format` 保持 omitted；
- per-call `thinking` disabled；
- `max_output_tokens=400`；
- 不重试，不读 development，不读 frozen。

以下动作仍然禁止：development 输入、Stage B、Stage C、生产入口切换、使用失败结果伪造语义指标。

复现命令：

```powershell
.venv\Scripts\python.exe tests\audit_compact_stage_a_protocol_k16.py
```
