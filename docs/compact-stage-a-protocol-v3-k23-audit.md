# K23 compact Stage-A protocol v3 离线授权审计

## 结论

K23 离线契约审计通过，输出为“有条件的一次性授权”，不是语义质量通过，也不是生产放行。

授权 ID：`K23_COMPACT_STAGE_A_HEALTH_V3`

只允许后续执行一次：

- `synthetic_health_only`
- 模型 `deepseek-v4-flash`
- `response_format` 省略（`omitted`，不得发送）
- 每次调用显式 `thinking` disabled
- `max_output_tokens=400`
- provider call 最多 1 次，retry=0
- 不读 development、private 正文、frozen/frozen_test
- Stage B、Stage C 永远为 false

该授权不能被解释为允许 development pilot、真实语义评估、事件/标题/首页接入或生产切换。

## 审计边界

独立测试文件为：

`D:\Project_Codex\Project_WeChatMoreFunction\tests\test_compact_stage_a_protocol_v3_k23_authorization.py`

测试只使用 synthetic records 和临时 SQLite ledger。它读取的历史材料仅是下列两个已经声明 body-free 的聚合审计摘要，不打开对应 artifact 的请求、响应、消息或其他正文文件：

- `compact_stage_a_protocol_health_v1/audit/audit_summary.private.json`
- `compact_stage_a_protocol_health_v2/audit/audit_summary.private.json`

未调用 provider，未导入或执行 development runner，未读取 frozen/frozen_test，未修改 v3 实现。

## 已核验项目

1. v3 输出使用完整可读字段：`topics`、`topic_id`、`primary_message_ids`、`context_message_ids`、`uncertainty`；最小 JSON 示例可以在本地严格解析。旧 v1/v2 单字母输出不被接受。
2. request/message/candidate/topic 均执行 exact-key 检查；Stage-B 的人物、对象、动作、状态、claim、modality、evidence 等字段被拒绝。
3. topic 数量受 primary 数量约束；每个 topic 必须有 primary；primary 必须覆盖且只分配一次；context 只能引用允许的消息且不能重复。问候/无 reply 候选仍保留在请求表和 ledger 中，不被凭空变成 topic。
4. forged alias、跨 scope handle、handle kind mismatch、重复 topic/primary/context、重复 JSON key 均 fail closed。
5. 最大 synthetic request（14 messages、20 candidates）完整 HTTP envelope 的 token proxy 不超过 1600；最大合法 v3 response 不超过 400。
6. cache namespace、protocol/prompt/cache version、model、ruleset、request hash 均参与 cache key；请求内容、模型和 ruleset 变化会改变 key，且与 v1/v2 key 隔离。
7. 默认 body-free ledger 只保留 scope、opaque handles、计数、hash、validated assignments 和尺寸统计；synthetic body marker 不会进入 ledger。健康 facade 只记录字段名/长度/hash/计数等诊断，不保存 provider response。
8. `compact_stage_a_protocol_health_v3.py` 的能力边界是 synthetic-only：无 HTTP/provider import、无 development/frozen 输入读取；协议、prompt、response_format omitted、thinking disabled、400、1 call、0 retry 与 v3 契约一致。Fake model 仅做内存协议检查。
9. 现有 durable `persistent_call_budget` 在临时 authority root 上以新授权 ID 成功建立 binding、消费 1 个 slot；重开同一 binding 后第二次 reserve 被拒绝，证明跨进程/跨输出目录的全局计数能力可接入。

历史 v1/v2 body-free 审计均显示：artifact 为 blocked、health 未完成、旧授权已关闭、development/Stage B/Stage C 均不放行，且 body/identity/reasoning/secret 命中为 0。K23 不复用旧授权 ID。

## 仍然必须遵守的硬条件

`compact_stage_a_protocol_health_v3.py` 本身是 synthetic health facade，不是 DeepSeek HTTP adapter，也没有直接集成 durable global ledger；因此后续真正执行 health-only 的外层 runner 必须在 provider 进入前接入 `persistent_call_budget`，使用上面的新授权 ID，并把 reservation/started/complete 或 failed 写入同一稳定 ledger。绕过该 ledger、换输出目录重跑、重复 health 或将诊断结果当作 development 结果，均视为未授权。

此外，`project_body_free_ledger` 的 `report` 参数必须只传入本地生成且已验证的 size report。当前 K23 默认路径已核验；若未来允许不可信调用方直接传入任意 mapping，应先增加 report schema/敏感字段拒绝测试，再宣称该可选路径具有通用 body-free 保证。

如果这一次 v3 health 返回 schema/JSON 失败、超限、provider incompatibility 或其他非 `strict_complete=true` 结果：

- 只保存 body-free blocked diagnostic；
- 不把 `diagnostic_candidate` 当成 accepted result；
- 不重试、不追加 provider 探测；
- 不读 development；
- 不放行 K11/K14 类 Stage-A development、Stage B/C 或生产；
- 需要新的离线修复和新的授权 ID 后才能再评估。

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_compact_stage_a_protocol_v3_k22.py tests/test_compact_stage_a_protocol_v3_k23_authorization.py tests/test_compact_stage_a_protocol_health.py tests/test_persistent_call_budget.py
```

K23 独立审计的通过只代表上述离线结构和资源边界成立。它没有证明 DeepSeek 的真实输出可被 v3 schema 接受，也没有产生 development 语义准确率、人物/对象/状态或端到端首页指标。
