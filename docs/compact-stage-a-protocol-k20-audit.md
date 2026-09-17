# K20：compact Stage-A protocol v2 离线授权审计

审计脚本：`D:\Project_Codex\Project_WeChatMoreFunction\tests\audit_compact_stage_a_protocol_v2_k20.py`

## 结论

K20 的离线协议门通过，结论为 `pass_offline_authorization_only`。这只允许
一个全新的、仅 synthetic health 的协议检查，不代表语义质量通过，也不代表
可以读取 development、运行 Stage B/C 或接入生产。

唯一建议放行的后续调用如下：

- 新 `authorization_id`：`K20_COMPACT_STAGE_A_HEALTH_V2`
- 模型：`deepseek-v4-flash`
- `response_format`：省略（`omitted`）
- 每次调用显式关闭 thinking
- `max_output_tokens=400`
- 最多 1 次 provider call，禁止 retry
- `synthetic_health_only=true`

仍然禁止：development 输入、frozen/frozen_test、Stage B、Stage C、生产写入、
任何把失败诊断当作完整结果的操作。实际 provider 调用必须由新的授权账本单独
记录，不能复用 K18 或把旧失败输出目录当作新预算。

## 审计范围与输入

本轮只读取和检查：

- `src/wechat_bridge/compact_stage_a_protocol.py` 的纯本地接口；
- K16 公共审计说明
  `D:\Project_Codex\Project_WeChatMoreFunction\docs\compact-stage-a-protocol-k16-audit.md`；
- K19 公共审计脚本和 K18 的 body-free 审计摘要
  `data\private\gold_standard\2026-08-25\compact_stage_a_protocol_health_v1\audit\audit_summary.private.json`。

没有导入 runner/provider，没有读取 development 或 frozen 数据，没有执行真实
provider 调用，也没有修改生产代码。所有消息 cue 和长字段只是 synthetic、仅在
内存中用于验证尺寸与 ledger 隔离。

## 关键离线结果

| 检查 | 结果 |
|---|---:|
| 最小包 primary/context | 1/1，topic_limit=1 |
| 最坏包消息/候选 | 14/20 |
| 最坏包 primary/topic_limit | 12/12 |
| 完整 HTTP input token proxy | 1530≤1600 |
| 最大合法 Stage-A response proxy | 135≤400 |
| primary 覆盖 | 每条恰好一次 |
| context 单独建 topic | 拒绝 |
| candidate/message 数量改变 topic_limit | 不改变 |
| cross-scope 请求/输出 | 拒绝 |
| Stage-B 字段混入输出 | 拒绝 |
| body-free ledger | 通过，synthetic body marker 未泄漏 |
| v1/v2 request、HTTP、prompt、cache-key material hash | 均不同，不复用 v1 namespace |
| provider calls | 0 |

`topic_limit=primary_count` 是同一个规则符号，已同时出现在 system prompt、
request schema、response schema，并由 validator 从 primary 投影动态计算。静态
检查确认该规则只定义一次，运行检查覆盖 primary=1、2、12 和相同 primary、不同
context/candidate 数量的包。

当前协议模块没有独立的持久缓存 API；因此审计把可复用的 cache key material
固定为 `protocol_version + prompt_version + request_sha256`，并验证 v1 与 v2 的
request、完整 HTTP、prompt 和该 key material 的 hash 均发生变化。未来若接入
缓存，必须使用这组版本化材料，不能只使用消息数量或旧 request hash。

## 拒绝边界

独立合成审计覆盖并保留稳定拒绝码：额外字段、空 topic、空 primary、重复
primary、伪造 alias、跨 scope alias、Stage-B 字段、跨 scope request、仅有
context 的 request，以及超过 primary 上限的 topic 数。`unknown` 仍可作为
不确定性值保留；协议不会在 Stage A 强行补出人物、对象、claim、state 或标题。

## 回归结果

K20 专项：

```text
15 passed
```

compact Stage-A 相关协议、K19 topic-limit、health 合计：

```text
52 passed
```

复现命令：

```powershell
.venv\Scripts\python.exe -m pytest -q tests\test_compact_stage_a_protocol_v2_k20_authorization.py
.venv\Scripts\python.exe tests\audit_compact_stage_a_protocol_v2_k20.py
```

## 仍未证明的事项

K20 只证明本地 wire 的边界、尺寸、版本/hash 隔离和保守校验；它没有证明
DeepSeek 能正确完成真实主题划分，也没有改变 K14 的严格 JSON 截断失败，更
没有评估人物、对象、状态、无 reply 承接、误拆/误合或证据质量。因此即使后续
一次 synthetic health 成功，也只能继续进入独立的、受预算约束的开发页试验，
不能直接切首页或生产入口。
